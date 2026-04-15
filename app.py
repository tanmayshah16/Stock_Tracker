"""
Stock Portfolio Tracker — Streamlit App
Modules:
  1. Dashboard           — Per-stock returns, allocation, XIRR
  2. Add Transaction     — Buy/Sell entry with NSE/BSE dropdown
  3. Active Trades       — Live P&L of holdings + stop-loss/target tracking
  4. Trade Journal       — Strategy, entry/exit reasons, rating per closed trade
  5. Capital Tracker     — Deposits/withdrawals + capital deployed view
  6. Daily Snapshot      — Auto-logged equity curve
"""

import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, date
from scipy.optimize import brentq
import plotly.express as px
import plotly.graph_objects as go
import time

# Plotly: transparent backgrounds so charts blend with light or dark theme
PLOTLY_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(family="-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"),
    xaxis=dict(gridcolor="rgba(127,127,127,0.15)", zerolinecolor="rgba(127,127,127,0.3)"),
    yaxis=dict(gridcolor="rgba(127,127,127,0.15)", zerolinecolor="rgba(127,127,127,0.3)"),
)

# ============================================================
# PAGE CONFIG & STYLING
# ============================================================
st.set_page_config(page_title="Portfolio Tracker", page_icon="📈", layout="wide")

st.markdown("""
<style>
    /* Layout */
    .main { padding-top: 1rem; }
    .block-container { padding-top: 2rem; padding-bottom: 2rem; max-width: 1400px; }

    /* Metric cards — theme-aware using semi-transparent backgrounds */
    [data-testid="stMetric"] {
        background: rgba(127, 127, 127, 0.08);
        border: 1px solid rgba(127, 127, 127, 0.2);
        padding: 18px 20px;
        border-radius: 12px;
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
        transition: transform 0.15s ease, box-shadow 0.15s ease;
    }
    [data-testid="stMetric"]:hover {
        transform: translateY(-2px);
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
    }
    [data-testid="stMetricLabel"] {
        font-weight: 600 !important;
        font-size: 0.85rem !important;
        opacity: 0.75;
        text-transform: uppercase;
        letter-spacing: 0.03em;
    }
    [data-testid="stMetricValue"] {
        font-size: 1.6rem !important;
        font-weight: 700 !important;
    }
    [data-testid="stMetricDelta"] {
        font-size: 0.9rem !important;
        font-weight: 600 !important;
    }

    /* Headings */
    h1 { font-weight: 700 !important; letter-spacing: -0.02em; }
    h2, h3 { font-weight: 600 !important; letter-spacing: -0.01em; }

    /* Buttons */
    .stButton button {
        border-radius: 8px;
        font-weight: 600;
        transition: all 0.15s ease;
        border: 1px solid rgba(127, 127, 127, 0.2);
    }
    .stButton button:hover { transform: translateY(-1px); }

    /* Tables */
    [data-testid="stDataFrame"] {
        border-radius: 10px;
        overflow: hidden;
        border: 1px solid rgba(127, 127, 127, 0.15);
    }

    /* Sidebar */
    [data-testid="stSidebar"] { padding-top: 1rem; }

    /* Dividers */
    hr { margin: 1.5rem 0 !important; opacity: 0.3; }
</style>
""", unsafe_allow_html=True)

# ============================================================
# CONFIG
# ============================================================
SHEET_NAME = "Portfolio_Tracker"
WS_TXNS = "Transactions"
WS_TARGETS = "Trade_Targets"
WS_JOURNAL = "Trade_Journal"
WS_CAPITAL = "Capital_Flows"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

WS_HEADERS = {
    WS_TXNS: ["Date", "Symbol", "Stock Name", "Exchange", "Action",
              "Quantity", "Price", "Total Value", "Notes"],
    WS_TARGETS: ["Symbol", "Stop Loss", "Target", "Updated"],
    WS_JOURNAL: ["Date", "Symbol", "Strategy", "Entry Reason",
                 "Exit Reason", "Rating", "P&L", "Notes"],
    WS_CAPITAL: ["Date", "Type", "Amount", "Notes"],
}

# ============================================================
# GOOGLE SHEETS
# ============================================================
@st.cache_resource
def get_gsheet_client():
    try:
        creds_dict = dict(st.secrets["gcp_service_account"])
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        return gspread.authorize(creds)
    except Exception as e:
        st.error(f"Google Sheets auth failed: {e}")
        return None


def get_or_create_sheet():
    client = get_gsheet_client()
    if client is None:
        return None
    try:
        try:
            sheet = client.open(SHEET_NAME)
        except gspread.SpreadsheetNotFound:
            sheet = client.create(SHEET_NAME)
            if "user_email" in st.secrets:
                sheet.share(st.secrets["user_email"], perm_type="user", role="writer")
        return sheet
    except Exception as e:
        st.error(f"Sheet error: {e}")
        return None


def get_or_create_ws(name: str):
    sheet = get_or_create_sheet()
    if sheet is None:
        return None
    try:
        try:
            ws = sheet.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = sheet.add_worksheet(title=name, rows=1000, cols=15)
            ws.append_row(WS_HEADERS[name])
        return ws
    except Exception as e:
        st.error(f"Worksheet '{name}' error: {e}")
        return None


def append_row(ws_name: str, row: list) -> bool:
    ws = get_or_create_ws(ws_name)
    if ws is None:
        return False
    try:
        ws.append_row(row, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        st.error(f"Failed to append to {ws_name}: {e}")
        return False


def upsert_target(symbol: str, sl: float, target: float) -> bool:
    ws = get_or_create_ws(WS_TARGETS)
    if ws is None:
        return False
    try:
        records = ws.get_all_records()
        for idx, r in enumerate(records, start=2):
            if str(r["Symbol"]).strip().upper() == symbol.upper():
                ws.update(f"A{idx}:D{idx}", [[symbol, sl, target,
                          datetime.now().strftime("%Y-%m-%d %H:%M")]])
                return True
        ws.append_row([symbol, sl, target,
                       datetime.now().strftime("%Y-%m-%d %H:%M")],
                      value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        st.error(f"Failed to save target: {e}")
        return False


@st.cache_data(ttl=60)
def load_ws(ws_name: str) -> pd.DataFrame:
    ws = get_or_create_ws(ws_name)
    if ws is None:
        return pd.DataFrame(columns=WS_HEADERS.get(ws_name, []))
    try:
        records = ws.get_all_records()
        if not records:
            return pd.DataFrame(columns=WS_HEADERS.get(ws_name, []))
        df = pd.DataFrame(records)
        if "Date" in df.columns:
            df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        for col in ["Quantity", "Price", "Total Value", "Amount",
                    "Stop Loss", "Target", "Rating", "P&L"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    except Exception as e:
        st.error(f"Load {ws_name} failed: {e}")
        return pd.DataFrame(columns=WS_HEADERS.get(ws_name, []))


# ============================================================
# STOCK UNIVERSE — fetch all NSE + BSE listed equities
# ============================================================
import io
import requests

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/json,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def _fetch_nse() -> pd.DataFrame:
    """NSE equity list. NSE blocks default UAs, so use a real browser session."""
    url = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
    sess = requests.Session()
    sess.headers.update(BROWSER_HEADERS)
    # Warm-up call to set cookies (NSE requires this)
    try:
        sess.get("https://www.nseindia.com", timeout=10)
    except Exception:
        pass
    r = sess.get(url, timeout=15)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = [c.strip() for c in df.columns]
    out = pd.DataFrame({
        "Symbol": df["SYMBOL"].astype(str).str.strip(),
        "Name": df["NAME OF COMPANY"].astype(str).str.strip(),
        "Exchange": "NSE",
        "YF_Ticker": df["SYMBOL"].astype(str).str.strip() + ".NS",
    })
    return out[out["Symbol"].str.len() > 0]


@st.cache_data(ttl=86400, show_spinner=False)
@st.cache_data(ttl=86400)
def load_stock_universe():
    try:
        df = pd.read_csv("nse_stocks.csv")

        # Clean columns
        df["Symbol"] = df["Symbol"].astype(str).str.strip().str.upper()
        df["Name"] = df["Name"].astype(str).str.strip()

        df["Exchange"] = "NSE"
        df["YF_Ticker"] = df["Symbol"] + ".NS"
        df["Display"] = df["Symbol"] + " — " + df["Name"]

        return df.sort_values("Symbol").reset_index(drop=True)

    except Exception as e:
        st.error(f"Error loading stock list: {e}")
        return pd.DataFrame(columns=["Symbol", "Name", "Exchange", "YF_Ticker", "Display"])
@st.cache_data(ttl=300)
def get_current_price(symbol, exchange="NSE"):
    import yfinance as yf

    ticker = f"{symbol}.NS" if exchange == "NSE" else f"{symbol}.BO"

    try:
        t = yf.Ticker(ticker)

        # Try 5d history (most reliable)
        hist = t.history(period="5d")

        if not hist.empty:
            price = hist["Close"].dropna().iloc[-1]
            if price > 0:
                return float(price)

    except Exception as e:
        print(f"❌ PRICE ERROR {symbol}: {e}")

    return None


# ============================================================
# RETURN CALCULATIONS
# ============================================================
def xnpv(rate, cashflows):
    if rate <= -1.0:
        return float("inf")
    t0 = cashflows[0][0]
    return sum(cf / (1 + rate) ** ((d - t0).days / 365.0) for d, cf in cashflows)

def xirr(cashflows):
    if len(cashflows) < 2:
        return None

    if not (any(c > 0 for _, c in cashflows) and any(c < 0 for _, c in cashflows)):
        return None

    try:
        return brentq(lambda r: xnpv(r, cashflows), -0.999, 10.0)
    except:
        try:
            return brentq(lambda r: xnpv(r, cashflows), -0.999, 1000.0)
        except:
            # ✅ FALLBACK (VERY IMPORTANT)
            # Approximate return for short duration trades
            d1, c1 = cashflows[0]
            d2, c2 = cashflows[-1]

            days = (d2 - d1).days

            if days <= 0:
                return None

            simple_return = (c2 / -c1) - 1

            # annualize
            approx_xirr = (1 + simple_return) ** (365 / days) - 1

            print("⚠️ Using fallback XIRR:", approx_xirr)

            return approx_xirr

def compute_stock_metrics(df: pd.DataFrame, universe: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    rows = []

    for symbol, group in df.groupby("Symbol"):
        group = group.sort_values("Date")

        exchange = group["Exchange"].iloc[0]
        name = group["Stock Name"].iloc[0]

        qty = 0.0
        invested = 0.0
        realized = 0.0
        avg_cost_qty = 0.0
        avg_cost_value = 0.0
        cashflows = []

        for _, r in group.iterrows():
            q = float(r["Quantity"])
            p = float(r["Price"])
            d = r["Date"].date()

            if r["Action"] == "BUY":
                qty += q
                invested += q * p
                avg_cost_qty += q
                avg_cost_value += q * p
                cashflows.append((d, -q * p))

            else:
                qty -= q
                realized += q * p

                if avg_cost_qty > 0:
                    avg_cost_value -= (avg_cost_value / avg_cost_qty) * q
                    avg_cost_qty -= q

                cashflows.append((d, q * p))

        # ✅ FIXED PRICE FETCH
        print(f"\nFetching price for {symbol}")
        cur_price = get_current_price(symbol, exchange)

        if cur_price is None:
            print(f"❌ Price missing for {symbol}")
            cur_price = 0

        cur_value = qty * cur_price

        # ✅ XIRR CALC
        cf_for_xirr = list(cashflows)

        if qty > 0 and cur_value > 0:
            cf_for_xirr.append((date.today(), cur_value))

        if not (any(c > 0 for _, c in cf_for_xirr) and any(c < 0 for _, c in cf_for_xirr)):
            x = None
        else:
            x = xirr(cf_for_xirr)

        pnl = (realized + cur_value) - invested
        abs_pct = (pnl / invested * 100) if invested > 0 else 0

        avg_cost = (avg_cost_value / avg_cost_qty) if avg_cost_qty > 0 else 0

        rows.append({
            "Symbol": symbol,
            "Name": name,
            "Exchange": exchange,
            "Net Qty": round(qty, 2),
            "Avg Cost": round(avg_cost, 2),
            "Invested": round(invested, 2),
            "Current Price": round(cur_price, 2) if cur_price else 0,
            "Current Value": round(cur_value, 2),
            "Realized": round(realized, 2),
            "P&L": round(pnl, 2),
            "Abs Return %": round(abs_pct, 2),
            "XIRR %": round(x * 100, 2) if x is not None and not np.isnan(x) else None
        })

    return pd.DataFrame(rows)


# ============================================================
# PAGE: DASHBOARD
# ============================================================
def page_dashboard(txns, universe):
    st.title("📈 Strategy Dashboard")

    if txns.empty:
        st.info("No transactions yet. Add one from **Add Transaction**.")
        return

    txns = txns.sort_values("Date")

    # =========================
    # SMART TRADE BUILDER
    # =========================
    trades = []

    for symbol, df_sym in txns.groupby("Symbol"):

        df_sym = df_sym.sort_values("Date")

        current_buy = None

        for _, row in df_sym.iterrows():

            if row["Action"] == "BUY":
                # If previous buy not closed → treat as new trade
                current_buy = row

            elif row["Action"] == "SELL" and current_buy is not None:
                trades.append([current_buy, row])
                current_buy = None

        # If BUY exists but no SELL → open trade
        if current_buy is not None:
            trades.append([current_buy])

    # =========================
    # CAPITAL
    # =========================
    first_buy = txns[txns["Action"] == "BUY"].iloc[0]
    capital = first_buy["Total Value"]

    total_pnl = 0
    current_trade_pnl = 0
    current_xirr = None

    trade_rows = []

    # =========================
    # PROCESS TRADES
    # =========================
    for trade in trades:
        buy = trade[0]

        symbol = buy["Symbol"]
        exchange = buy["Exchange"]
        qty = buy["Quantity"]
        buy_value = buy["Total Value"]
        buy_date = buy["Date"]

        sell = next((t for t in trade if t["Action"] == "SELL"), None)

        print("\n======================")
        print("SYMBOL:", symbol)

        if sell is not None:
            sell_value = sell["Total Value"]
            sell_date = sell["Date"]

            pnl = sell_value - buy_value
            total_pnl += pnl

            abs_return = (pnl / buy_value) * 100 if buy_value > 0 else 0

            cashflows = [
                (buy_date.date(), -buy_value),
                (sell_date.date(), sell_value)
            ]

        else:
            current_price = get_current_price(symbol, exchange)

            print("CURRENT PRICE:", current_price)

            if current_price is None:
                current_price = 0

            current_value = qty * current_price

            pnl = current_value - buy_value
            total_pnl += pnl
            current_trade_pnl = pnl

            abs_return = (pnl / buy_value) * 100 if buy_value > 0 else 0

            cashflows = [
                (buy_date.date(), -buy_value),
                (date.today(), current_value)
            ]

            current_xirr = xirr(cashflows)

            sell_value = current_value
            sell_date = "OPEN"

        # ✅ SAFE XIRR
        if not (any(c > 0 for _, c in cashflows) and any(c < 0 for _, c in cashflows)):
            trade_xirr = None
        else:
            trade_xirr = xirr(cashflows)

        print("CASHFLOWS:", cashflows)
        print("XIRR:", trade_xirr)

        trade_rows.append({
            "Symbol": symbol,
            "Buy Date": buy_date.date(),
            "Sell Date": sell_date if sell_date == "OPEN" else sell_date.date(),
            "Buy Value": buy_value,
            "Sell Value": sell_value,
            "P&L": pnl,
            "Abs Return %": abs_return,
            "XIRR %": trade_xirr * 100 if trade_xirr is not None and not np.isnan(trade_xirr) else None
        })
    # =========================
    # METRICS
    # =========================
    pnl_pct = (total_pnl / capital * 100) if capital > 0 else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("💰 Capital", f"₹{capital:,.0f}")
    c2.metric("📊 Total P&L", f"₹{total_pnl:,.0f}", f"{pnl_pct:.2f}%")
    c3.metric("🎯 Current Trade P&L", f"₹{current_trade_pnl:,.0f}")

    xirr_display = (
        f"{current_xirr*100:.2f}%"
        if current_xirr is not None and not np.isnan(current_xirr)
        else "—"
    )
    c4.metric("⏱️ Current XIRR", xirr_display)

    st.divider()

    # =========================
    # TRADE HISTORY (MOVED UP)
    # =========================
    st.subheader("📋 Trade History")

    tdf = pd.DataFrame(trade_rows)

    if not tdf.empty:
        show = tdf.copy()
        show["Buy Date"] = show["Buy Date"].astype(str)
        show["Sell Date"] = show["Sell Date"].astype(str)

        for col in ["Buy Value", "Sell Value", "P&L"]:
            show[col] = show[col].apply(lambda x: f"₹{x:,.0f}")

        show["Abs Return %"] = show["Abs Return %"].apply(lambda x: f"{x:.2f}%")
        show["XIRR %"] = show["XIRR %"].apply(
            lambda x: f"{x:.2f}%" if pd.notna(x) else "—"
        )

        st.dataframe(show, use_container_width=True, hide_index=True)

    st.divider()

    # =========================
    # TRADE P&L (MOVED DOWN)
    # =========================
    st.subheader("📊 Trade-wise P&L")

    if not tdf.empty:
        colors = ["#ef4444" if x < 0 else "#22c55e" for x in tdf["P&L"]]

        fig = go.Figure(go.Bar(
            x=tdf["Symbol"],
            y=tdf["P&L"],
            marker_color=colors,
            text=[f"₹{x:,.0f}" for x in tdf["P&L"]],
            textposition="outside"
        ))

        fig.update_layout(**PLOTLY_LAYOUT, height=350)
        st.plotly_chart(fig, use_container_width=True)


# ============================================================
# PAGE: ADD TRANSACTION
# ============================================================
def page_add_transaction(universe):
    st.title("➕ Add Transaction")
    col1, col2 = st.columns([2, 1])

    with col1:
        selected = st.selectbox("Stock", options=universe["Display"].tolist(),
                                index=None, placeholder="Type to search NSE/BSE stocks...")
        action = st.radio("Action", ["BUY", "SELL"], horizontal=True)
        c1, c2 = st.columns(2)
        qty = c1.number_input("Quantity", min_value=0.0, step=1.0, value=1.0)
        price = c2.number_input("Price (₹)", min_value=0.0, step=0.05,
                                value=0.0, format="%.2f")
        txn_date = st.date_input("Date", value=date.today(), max_value=date.today())
        notes = st.text_input("Notes (optional)", "")

    with col2:
        if selected:
            row = universe[universe["Display"] == selected].iloc[0]
            live = get_current_price(row["YF_Ticker"])
            st.metric("Live Price", f"₹{live:.2f}" if live > 0 else "N/A")
            st.caption(f"Exchange: **{row['Exchange']}**")
            if qty > 0 and price > 0:
                st.metric("Total Value", f"₹{qty * price:,.2f}")

    if st.button("Add Transaction", type="primary", width='stretch'):
        if not selected:
            st.error("Pick a stock"); return
        if qty <= 0 or price <= 0:
            st.error("Qty & price must be > 0"); return
        row = universe[universe["Display"] == selected].iloc[0]
        ok = append_row(WS_TXNS, [
            txn_date.strftime("%Y-%m-%d"), row["Symbol"], row["Name"],
            row["Exchange"], action, qty, price, qty * price, notes,
        ])
        if ok:
            st.success(f"✅ {action} {qty} {row['Symbol']} @ ₹{price} — redirecting to Dashboard...")
            st.cache_data.clear()
            st.session_state["page"] = "📈 Dashboard"
            time.sleep(1.0)
            st.rerun()


# ============================================================
# PAGE: ACTIVE TRADES
# ============================================================
def page_active_trades(metrics):
    st.title("🎯 Active Trades")
    if metrics.empty or metrics[metrics["Net Qty"] > 0].empty:
        st.info("No active holdings.")
        return

    active = metrics[metrics["Net Qty"] > 0].copy()
    targets = load_ws(WS_TARGETS)

    if not targets.empty:
        active = active.merge(
            targets[["Symbol", "Stop Loss", "Target"]], on="Symbol", how="left"
        )
    else:
        active["Stop Loss"] = np.nan
        active["Target"] = np.nan

    def status(row):
        cp = row["Current Price"]
        if pd.notna(row["Stop Loss"]) and row["Stop Loss"] > 0 and cp <= row["Stop Loss"]:
            return "🔴 SL HIT"
        if pd.notna(row["Target"]) and row["Target"] > 0 and cp >= row["Target"]:
            return "🟢 TARGET HIT"
        if pd.notna(row["Stop Loss"]) and row["Stop Loss"] > 0:
            buffer = (cp - row["Stop Loss"]) / cp * 100
            if buffer < 2:
                return f"🟡 Near SL ({buffer:.1f}%)"
        return "✅ Active"

    active["Status"] = active.apply(status, axis=1)
    active["Risk per Share"] = active.apply(
        lambda r: r["Avg Cost"] - r["Stop Loss"] if pd.notna(r["Stop Loss"]) else np.nan,
        axis=1,
    )
    active["Total Risk"] = active["Risk per Share"] * active["Net Qty"]
    active["R:R"] = active.apply(
        lambda r: ((r["Target"] - r["Avg Cost"]) / (r["Avg Cost"] - r["Stop Loss"]))
        if pd.notna(r["Target"]) and pd.notna(r["Stop Loss"]) and (r["Avg Cost"] - r["Stop Loss"]) > 0
        else np.nan, axis=1,
    )

    total_value = active["Current Value"].sum()
    total_pnl = (active["Current Value"] - active["Net Qty"] * active["Avg Cost"]).sum()
    total_risk = active["Total Risk"].dropna().sum()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Holdings", len(active))
    c2.metric("Current Value", f"₹{total_value:,.0f}")
    c3.metric("Unrealized P&L", f"₹{total_pnl:,.0f}",
              f"{total_pnl/active['Invested'].sum()*100:.2f}%" if active['Invested'].sum() > 0 else "")
    c4.metric("Total Capital at Risk", f"₹{total_risk:,.0f}" if total_risk else "—")

    st.divider()
    st.subheader("📊 Active Positions")
    show = active[["Symbol", "Net Qty", "Avg Cost", "Current Price",
                   "Stop Loss", "Target", "Total Risk", "R:R",
                   "Current Value", "Status"]].copy()
    for col in ["Avg Cost", "Current Price", "Stop Loss", "Target",
                "Total Risk", "Current Value"]:
        show[col] = show[col].apply(lambda x: f"₹{x:,.2f}" if pd.notna(x) else "—")
    show["R:R"] = show["R:R"].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "—")
    st.dataframe(show, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("✏️ Set / Update Stop-Loss & Target")
    sym = st.selectbox("Symbol", active["Symbol"].tolist())
    cur_row = active[active["Symbol"] == sym].iloc[0]
    avg = float(cur_row["Avg Cost"])
    cur = float(cur_row["Current Price"])
    existing_sl = float(cur_row["Stop Loss"]) if pd.notna(cur_row["Stop Loss"]) else max(0.0, avg * 0.95)
    existing_tg = float(cur_row["Target"]) if pd.notna(cur_row["Target"]) else avg * 1.15

    c1, c2 = st.columns(2)
    sl = c1.number_input(f"Stop Loss (avg ₹{avg:.2f}, cur ₹{cur:.2f})",
                         min_value=0.0, value=existing_sl, step=0.5, format="%.2f")
    tg = c2.number_input("Target", min_value=0.0, value=existing_tg, step=0.5, format="%.2f")

    if sl > 0 and tg > 0 and avg > 0:
        risk = avg - sl
        reward = tg - avg
        rr = reward / risk if risk > 0 else 0
        st.caption(f"Risk: ₹{risk:.2f}/share • Reward: ₹{reward:.2f}/share • R:R = **{rr:.2f}**")

    if st.button("Save SL/Target", type="primary"):
        if upsert_target(sym, sl, tg):
            st.success(f"Saved for {sym}"); st.cache_data.clear()
            time.sleep(0.5); st.rerun()


# ============================================================
# PAGE: TRADE JOURNAL
# ============================================================
def page_journal(txns, metrics):
    st.title("📓 Trade Journal")
    st.caption("Log strategy + entry/exit reasoning + execution rating for closed trades.")

    journal = load_ws(WS_JOURNAL)
    sell_txns = txns[txns["Action"] == "SELL"].copy() if not txns.empty else pd.DataFrame()

    with st.expander("➕ New Journal Entry", expanded=False):
        if sell_txns.empty:
            st.info("No SELL transactions yet to journal.")
        else:
            sym_options = sell_txns["Symbol"].unique().tolist()
            sym = st.selectbox("Symbol", sym_options)
            jdate = st.date_input("Trade close date", value=date.today())
            strategy = st.selectbox("Strategy", [
                "Swing", "Positional", "Momentum", "Breakout",
                "Mean Reversion", "Investing", "Other",
            ])
            entry_reason = st.text_area("Entry Reason", height=80,
                                        placeholder="Why did you enter? (setup, signal, thesis)")
            exit_reason = st.text_area("Exit Reason", height=80,
                                       placeholder="Why did you exit? (target, SL, thesis change)")
            rating = st.slider("Execution Rating (1=poor, 5=perfect)", 1, 5, 3)
            pnl = st.number_input("Realized P&L (₹)", value=0.0, step=100.0)
            notes = st.text_input("Notes (optional)")
            if st.button("Save Entry", type="primary"):
                ok = append_row(WS_JOURNAL, [
                    jdate.strftime("%Y-%m-%d"), sym, strategy,
                    entry_reason, exit_reason, rating, pnl, notes,
                ])
                if ok:
                    st.success("Saved!"); st.cache_data.clear()
                    time.sleep(0.5); st.rerun()

    if journal.empty:
        st.info("No journal entries yet.")
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Trades Logged", len(journal))
    c2.metric("Avg Rating", f"{journal['Rating'].mean():.2f} / 5")
    wins = (journal["P&L"] > 0).sum()
    win_rate = wins / len(journal) * 100 if len(journal) > 0 else 0
    c3.metric("Win Rate", f"{win_rate:.1f}%")
    c4.metric("Total Realized P&L", f"₹{journal['P&L'].sum():,.0f}")

    st.divider()
    cl, cr = st.columns(2)
    with cl:
        st.subheader("📊 P&L by Strategy")
        by_strat = journal.groupby("Strategy")["P&L"].sum().reset_index()
        colors = ["#ef4444" if x < 0 else "#22c55e" for x in by_strat["P&L"]]
        fig = go.Figure(go.Bar(x=by_strat["Strategy"], y=by_strat["P&L"],
                               marker_color=colors,
                               text=[f"₹{x:,.0f}" for x in by_strat["P&L"]],
                               textposition="outside"))
        fig.update_layout(**PLOTLY_LAYOUT, height=350, margin=dict(t=20, b=20, l=20, r=20))
        st.plotly_chart(fig, use_container_width=True)
    with cr:
        st.subheader("⭐ Rating Distribution")
        rd = journal["Rating"].value_counts().sort_index().reset_index()
        rd.columns = ["Rating", "Count"]
        fig = px.bar(rd, x="Rating", y="Count",
                     color_discrete_sequence=["#3b82f6"])
        fig.update_layout(**PLOTLY_LAYOUT, height=350, margin=dict(t=20, b=20, l=20, r=20))
        st.plotly_chart(fig, use_container_width=True)

    st.subheader("📜 Journal Entries")
    show = journal.sort_values("Date", ascending=False).copy()
    show["Date"] = show["Date"].dt.strftime("%Y-%m-%d")
    show["P&L"] = show["P&L"].apply(lambda x: f"₹{x:,.0f}")
    st.dataframe(show, use_container_width=True, hide_index=True)


# ============================================================
# PAGE: CAPITAL TRACKER (auto-calculated from transactions)
# ============================================================
def page_capital(txns, metrics):
    st.title("💵 Capital Tracker")
    st.caption("Capital deployed = |Total Buys − Total Sells|. Auto-computed from your transactions.")

    if txns.empty:
        st.info("No transactions yet.")
        return

    # Build cumulative cashflow series
    df = txns.copy().sort_values("Date")
    df["Date"] = pd.to_datetime(df["Date"])
    df["Signed"] = np.where(df["Action"] == "BUY", df["Total Value"], -df["Total Value"])
    # Daily aggregation
    daily = df.groupby(df["Date"].dt.date)["Signed"].sum().reset_index()
    daily.columns = ["Date", "Net Flow"]
    daily["Date"] = pd.to_datetime(daily["Date"])

    # Build complete daily index from first txn to today, forward-fill
    full_idx = pd.date_range(daily["Date"].min(), pd.Timestamp(date.today()), freq="D")
    daily = daily.set_index("Date").reindex(full_idx, fill_value=0).rename_axis("Date").reset_index()
    daily["Cumulative Buy-Sell"] = daily["Net Flow"].cumsum()
    daily["Capital Deployed"] = daily["Cumulative Buy-Sell"].abs()

    # Top metrics
    total_buy = df.loc[df["Action"] == "BUY", "Total Value"].sum()
    total_sell = df.loc[df["Action"] == "SELL", "Total Value"].sum()
    capital_deployed = abs(total_buy - total_sell)

    # Total P&L = Realized (from sells) + Unrealized (from holdings)
    # Realized = total_sell - cost_basis_of_sold; for simplicity using metrics
    if not metrics.empty:
        realized = float(metrics["Realized"].sum()) - float(
            (metrics["Invested"] - metrics["Net Qty"] * metrics["Avg Cost"]).sum()
        )
        live_value = float((metrics["Net Qty"] * metrics["Current Price"]).sum())
        cost_of_holdings = float((metrics["Net Qty"] * metrics["Avg Cost"]).sum())
        unrealized = live_value - cost_of_holdings
        total_pnl = float(metrics["P&L"].sum())
    else:
        total_pnl = 0
        live_value = 0

    # Current Value as you defined: |Buys| + (Profit/Loss)
    current_value = total_buy + total_pnl
    pnl_pct = (total_pnl / total_buy * 100) if total_buy > 0 else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("💸 Total Buys", f"₹{total_buy:,.0f}")
    c2.metric("💰 Total Sells", f"₹{total_sell:,.0f}")
    c3.metric("🏦 Capital Deployed", f"₹{capital_deployed:,.0f}")
    c4.metric("📊 Current Value", f"₹{current_value:,.0f}",
              f"{pnl_pct:+.2f}%" if total_buy > 0 else "")

    st.divider()

    # Toggle: Day vs Month
    view = st.radio("View", ["Daily", "Monthly"], horizontal=True, key="capital_view")

    if view == "Daily":
        plot_df = daily.copy()
        x_label = "Date"
    else:
        # Resample to month-end, taking last value of each month (cumulative)
        m = daily.set_index("Date").resample("ME")["Capital Deployed"].last().reset_index()
        m["Date"] = m["Date"].dt.strftime("%b %Y")
        plot_df = m
        x_label = "Month"

    st.subheader(f"📈 Capital Deployed — {view} View")
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=plot_df["Date"], y=plot_df["Capital Deployed"],
        mode="lines+markers" if view == "Monthly" else "lines",
        line=dict(color="#3b82f6", width=2.5),
        fill="tozeroy", fillcolor="rgba(59,130,246,0.12)",
        name="Capital Deployed",
        hovertemplate="<b>%{x}</b><br>₹%{y:,.0f}<extra></extra>",
    ))
    fig.update_layout(**PLOTLY_LAYOUT, 
        height=420, margin=dict(t=20, b=20, l=20, r=20),
        xaxis_title=x_label, yaxis_title="₹",
        hovermode="x unified",
    )
    st.plotly_chart(fig, width='stretch')

    # Monthly buy vs sell breakdown
    st.subheader("📊 Monthly Buy vs Sell Activity")
    m_df = df.copy()
    m_df["Month"] = m_df["Date"].dt.to_period("M").dt.to_timestamp()
    monthly_act = m_df.groupby(["Month", "Action"])["Total Value"].sum().unstack(fill_value=0).reset_index()
    monthly_act["Month"] = monthly_act["Month"].dt.strftime("%b %Y")
    fig2 = go.Figure()
    if "BUY" in monthly_act.columns:
        fig2.add_trace(go.Bar(x=monthly_act["Month"], y=monthly_act["BUY"],
                              name="Buy", marker_color="#22c55e"))
    if "SELL" in monthly_act.columns:
        fig2.add_trace(go.Bar(x=monthly_act["Month"], y=monthly_act["SELL"],
                              name="Sell", marker_color="#ef4444"))
    fig2.update_layout(**PLOTLY_LAYOUT, barmode="group", height=350,
                       margin=dict(t=20, b=20, l=20, r=20),
                       yaxis_title="₹")
    st.plotly_chart(fig2, width='stretch')


# ============================================================
# ============================================================
# MAIN
# ============================================================
def main():
    with st.spinner("Loading..."):
        universe = load_stock_universe()
        txns = load_ws(WS_TXNS)

    metrics = compute_stock_metrics(txns, universe) if not txns.empty else pd.DataFrame()

    st.sidebar.title("📊 Navigation")
    pages = [
        "📈 Dashboard",
        "➕ Add Transaction",
        "🎯 Active Trades",
        "📓 Trade Journal",
        "💵 Capital Tracker",
    ]
    if "page" not in st.session_state:
        st.session_state["page"] = pages[0]
    page = st.sidebar.radio(
        "Page", pages,
        index=pages.index(st.session_state["page"]),
        key="page",
        label_visibility="collapsed",
    )

    st.sidebar.divider()
    st.sidebar.caption(f"📚 {len(universe):,} stocks loaded")
    st.sidebar.caption(f"🧾 {len(txns)} transactions")
    if st.sidebar.button("🔄 Refresh Data", width='stretch'):
        st.cache_data.clear(); st.rerun()

    if page == "📈 Dashboard":
        page_dashboard(txns, universe)
    elif page == "➕ Add Transaction":
        page_add_transaction(universe)
    elif page == "🎯 Active Trades":
        page_active_trades(metrics)
    elif page == "📓 Trade Journal":
        page_journal(txns, metrics)
    elif page == "💵 Capital Tracker":
        page_capital(txns, metrics)


if __name__ == "__main__":
    main()