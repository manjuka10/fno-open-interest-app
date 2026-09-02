import streamlit as st
import pandas as pd
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
import time

st.set_page_config(
    page_title="F&O Open Interest",
    page_icon="📊",
    layout="wide"
)

st.title("📊 F&O Open Interest")

NSE_HOME = "https://www.nseindia.com"
NSE_OPTION_CHAIN_PAGE = "https://www.nseindia.com/option-chain"
NSE_UNDERLYING_API = "https://www.nseindia.com/api/underlying-information"
NSE_OPTION_CHAIN_API = "https://www.nseindia.com/api/option-chain-equities"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_OPTION_CHAIN_PAGE,
    "Connection": "keep-alive",
}


def create_nse_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.get(NSE_HOME, timeout=15)
    time.sleep(0.5)
    session.get(NSE_OPTION_CHAIN_PAGE, timeout=15)
    return session


@st.cache_data(ttl=3600, show_spinner=False)
def get_fno_stocks():
    session = create_nse_session()

    response = session.get(
        NSE_UNDERLYING_API,
        timeout=20
    )
    response.raise_for_status()
    data = response.json()

    underlying_list = (
        data.get("data", {})
            .get("UnderlyingList", [])
    )

    stocks = []

    for item in underlying_list:
        symbol = (
            item.get("symbol")
            or item.get("SYMBOL")
            or item.get("underlying")
            or item.get("UNDERLYING")
        )

        if symbol:
            stocks.append(str(symbol).upper().strip())

    stocks = sorted(set(stocks))

    if len(stocks) < 100:
        raise RuntimeError(
            f"NSE returned only {len(stocks)} F&O stocks. "
            "Please try Refresh again."
        )

    return stocks


def get_option_chain(symbol):
    session = create_nse_session()

    response = session.get(
        NSE_OPTION_CHAIN_API,
        params={"symbol": symbol},
        timeout=20
    )
    response.raise_for_status()

    data = response.json()
    records = data.get("records", {})
    option_data = records.get("data", [])

    if not option_data:
        raise RuntimeError(
            f"No option-chain data returned by NSE for {symbol}."
        )

    nse_timestamp = (
        records.get("timestamp")
        or data.get("timestamp")
        or ""
    )

    expiry_dates = records.get("expiryDates", [])

    if not expiry_dates:
        raise RuntimeError(
            f"No expiry information returned by NSE for {symbol}."
        )

    nearest_expiry = expiry_dates[0]
    rows = []

    for item in option_data:
        strike = item.get("strikePrice")
        expiry = item.get("expiryDate")

        if expiry != nearest_expiry:
            continue

        ce = item.get("CE", {})
        pe = item.get("PE", {})

        rows.append({
            "Strike": strike,
            "CE OI": ce.get("openInterest", 0),
            "CE Change OI": ce.get("changeinOpenInterest", 0),
            "PE OI": pe.get("openInterest", 0),
            "PE Change OI": pe.get("changeinOpenInterest", 0),
        })

    df = pd.DataFrame(rows)

    if df.empty:
        raise RuntimeError(
            f"No option data available for nearest expiry "
            f"{nearest_expiry}."
        )

    numeric_cols = [
        "Strike",
        "CE OI",
        "CE Change OI",
        "PE OI",
        "PE Change OI"
    ]

    for col in numeric_cols:
        df[col] = pd.to_numeric(
            df[col],
            errors="coerce"
        ).fillna(0)

    return (
        df.sort_values("Strike").reset_index(drop=True),
        nearest_expiry,
        nse_timestamp
    )


def top_3_highest_oi(df, option_type):
    oi_col = f"{option_type} OI"

    result = (
        df[df[oi_col] > 0]
        .sort_values(oi_col, ascending=False)
        .head(3)
        [["Strike", oi_col]]
        .copy()
    )

    result.insert(0, "Rank", range(1, len(result) + 1))
    result.columns = ["Rank", "Strike", "Open Interest"]
    return result


def top_3_addition(df, option_type):
    change_col = f"{option_type} Change OI"

    result = (
        df[df[change_col] > 0]
        .sort_values(change_col, ascending=False)
        .head(3)
        [["Strike", change_col]]
        .copy()
    )

    result.insert(0, "Rank", range(1, len(result) + 1))
    result.columns = ["Rank", "Strike", "OI Addition"]
    return result


def top_3_unwinding(df, option_type):
    change_col = f"{option_type} Change OI"

    result = (
        df[df[change_col] < 0]
        .sort_values(change_col, ascending=True)
        .head(3)
        [["Strike", change_col]]
        .copy()
    )

    result["OI Unwinding"] = result[change_col].abs()
    result.insert(0, "Rank", range(1, len(result) + 1))

    return result[["Rank", "Strike", "OI Unwinding"]]


def format_oi_table(df):
    if df.empty:
        return df

    result = df.copy()

    if "Open Interest" in result.columns:
        result["Open Interest"] = result["Open Interest"].map(
            lambda x: f"{int(x):,}"
        )

    if "OI Addition" in result.columns:
        result["OI Addition"] = result["OI Addition"].map(
            lambda x: f"{int(x):+,}"
        )

    if "OI Unwinding" in result.columns:
        result["OI Unwinding"] = result["OI Unwinding"].map(
            lambda x: f"{int(x):,}"
        )

    result["Strike"] = result["Strike"].map(
        lambda x: f"{x:,.0f}"
    )

    return result


try:
    fno_stocks = get_fno_stocks()
except Exception as e:
    st.error(f"Unable to load NSE F&O stock universe: {e}")
    st.stop()


col1, col2 = st.columns([5, 1])

with col1:
    search_text = st.text_input(
        "Search Stock",
        placeholder="Enter F&O stock symbol e.g. RELIANCE",
        label_visibility="collapsed"
    ).strip().upper()

with col2:
    refresh = st.button(
        "🔄 Refresh",
        use_container_width=True
    )

if refresh:
    st.cache_data.clear()
    st.rerun()

if not search_text:
    st.info(
        f"Enter an F&O stock symbol to view Open Interest analysis. "
        f"Universe contains {len(fno_stocks)} F&O stocks."
    )
    st.stop()

if search_text in fno_stocks:
    selected_symbol = search_text
else:
    matches = [x for x in fno_stocks if search_text in x]

    if len(matches) == 1:
        selected_symbol = matches[0]
    elif len(matches) > 1:
        st.warning(
            "Multiple stocks found. Please enter the exact symbol:"
        )
        st.write(", ".join(matches[:30]))
        st.stop()
    else:
        st.error(
            f"{search_text} is not in the current NSE F&O universe."
        )
        st.stop()


with st.spinner(
    f"Fetching NSE option-chain data for {selected_symbol}..."
):
    try:
        df, expiry, nse_timestamp = get_option_chain(
            selected_symbol
        )
    except Exception as e:
        st.error(f"Unable to fetch option-chain data: {e}")
        st.info(
            "NSE can occasionally reject requests temporarily. "
            "Please press Refresh and try again."
        )
        st.stop()


if nse_timestamp:
    last_updated = nse_timestamp
else:
    last_updated = datetime.now(
        ZoneInfo("Asia/Kolkata")
    ).strftime("%d-%b-%Y %H:%M:%S IST")


st.markdown(f"### {selected_symbol}")

info1, info2, info3 = st.columns(3)

with info1:
    st.metric("Nearest Expiry", expiry)

with info2:
    st.metric("Last Updated", last_updated)

with info3:
    st.metric("Strikes Analysed", f"{len(df):,}")

st.divider()

st.subheader("📈 Top 3 Highest Open Interest")

ce_oi = top_3_highest_oi(df, "CE")
pe_oi = top_3_highest_oi(df, "PE")

col1, col2 = st.columns(2)

with col1:
    st.markdown("#### CE")
    st.dataframe(
        format_oi_table(ce_oi),
        use_container_width=True,
        hide_index=True
    )

with col2:
    st.markdown("#### PE")
    st.dataframe(
        format_oi_table(pe_oi),
        use_container_width=True,
        hide_index=True
    )

st.subheader("🟢 Top 3 Highest OI Addition")

ce_add = top_3_addition(df, "CE")
pe_add = top_3_addition(df, "PE")

col1, col2 = st.columns(2)

with col1:
    st.markdown("#### CE")
    st.dataframe(
        format_oi_table(ce_add),
        use_container_width=True,
        hide_index=True
    )

with col2:
    st.markdown("#### PE")
    st.dataframe(
        format_oi_table(pe_add),
        use_container_width=True,
        hide_index=True
    )

st.subheader("🔴 Top 3 Highest OI Unwinding")

ce_unwind = top_3_unwinding(df, "CE")
pe_unwind = top_3_unwinding(df, "PE")

col1, col2 = st.columns(2)

with col1:
    st.markdown("#### CE")
    st.dataframe(
        format_oi_table(ce_unwind),
        use_container_width=True,
        hide_index=True
    )

with col2:
    st.markdown("#### PE")
    st.dataframe(
        format_oi_table(pe_unwind),
        use_container_width=True,
        hide_index=True
    )

st.caption(
    "Data source: NSE India • OI values are in contracts • "
    "Analysis uses the nearest available expiry."
)
