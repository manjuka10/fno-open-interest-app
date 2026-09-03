import streamlit as st
import pandas as pd
try:
    from curl_cffi import requests
except ImportError:
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
NSE_CONTRACT_INFO_API = "https://www.nseindia.com/api/option-chain-contract-info"
NSE_OPTION_CHAIN_API = "https://www.nseindia.com/api/option-chain-v3"

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
    "X-Requested-With": "XMLHttpRequest",
}


def create_nse_session():
    try:
        session = requests.Session(impersonate="chrome")
    except TypeError:
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


def get_contract_info(session, symbol):
    response = session.get(
        NSE_CONTRACT_INFO_API,
        params={"symbol": symbol},
        timeout=25
    )
    response.raise_for_status()
    payload = response.json()
    expiry_dates = (
        payload.get("expiryDates")
        or payload.get("records", {}).get("expiryDates", [])
    )
    if not expiry_dates:
        raise RuntimeError(f"NSE returned no expiry dates for {symbol}.")
    return expiry_dates


def get_option_chain(symbol, expiry):
    last_error = None

    for attempt in range(4):
        try:
            session = create_nse_session()
            response = session.get(
                NSE_OPTION_CHAIN_API,
                params={
                    "type": "Equity",
                    "symbol": symbol,
                    "expiry": expiry,
                },
                timeout=30
            )
            response.raise_for_status()
            payload = response.json()

            records = payload.get("records", {})
            rows = records.get("data", []) or payload.get("data", [])

            if not rows:
                raise RuntimeError(
                    f"NSE returned no option-chain records for "
                    f"{symbol} ({expiry})."
                )

            parsed = []
            underlying_price = None

            for item in rows:
                if item.get("expiryDate") not in (None, expiry):
                    continue

                strike = item.get("strikePrice")
                if strike is None:
                    continue

                ce = item.get("CE") or item.get("ce") or {}
                pe = item.get("PE") or item.get("pe") or {}

                if underlying_price is None:
                    underlying_price = (
                        ce.get("underlyingValue")
                        or pe.get("underlyingValue")
                        or item.get("underlyingValue")
                    )

                parsed.append({
                    "Strike": strike,
                    "CE OI": ce.get("openInterest", 0),
                    "CE Change OI": ce.get("changeinOpenInterest", 0),
                    "PE OI": pe.get("openInterest", 0),
                    "PE Change OI": pe.get("changeinOpenInterest", 0),
                })

            df = pd.DataFrame(parsed)

            if df.empty:
                raise RuntimeError(
                    f"No option contracts found for {symbol} ({expiry})."
                )

            for col in [
                "Strike", "CE OI", "CE Change OI",
                "PE OI", "PE Change OI"
            ]:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

            previous_close = None
            price_change_pct = None

            # --------------------------------------------------------
            # LIVE EQUITY PRICE + CHANGE
            # --------------------------------------------------------
            # During market hours, get the live equity quote directly
            # from NSE. This is intentionally separate from the option
            # chain API because the option chain's underlying value is
            # not the correct source for live percentage change.
            live_quote_ok = False

            for quote_attempt in range(3):
                try:
                    quote_page = (
                        "https://www.nseindia.com/get-quotes/equity"
                        f"?symbol={symbol}"
                    )

                    quote_session = create_nse_session()
                    quote_session.headers.update({
                        "Referer": quote_page,
                        "Accept": "application/json,text/plain,*/*",
                        "X-Requested-With": "XMLHttpRequest",
                    })

                    quote_response = quote_session.get(
                        "https://www.nseindia.com/api/quote-equity",
                        params={"symbol": symbol},
                        timeout=25
                    )
                    quote_response.raise_for_status()
                    quote_payload = quote_response.json()

                    price_info = (
                        quote_payload.get("priceInfo", {})
                        if isinstance(quote_payload, dict)
                        else {}
                    )

                    live_price = (
                        price_info.get("lastPrice")
                        or price_info.get("lastTradedPrice")
                        or price_info.get("ltp")
                    )

                    live_pchange = (
                        price_info.get("pChange")
                        if price_info.get("pChange") is not None
                        else price_info.get("percentageChange")
                    )

                    live_prev_close = (
                        price_info.get("previousClose")
                        or price_info.get("prevClose")
                    )

                    if live_price is not None:
                        underlying_price = float(
                            str(live_price).replace(",", "")
                        )

                    if live_prev_close is not None:
                        previous_close = float(
                            str(live_prev_close).replace(",", "")
                        )

                    if live_pchange is not None:
                        price_change_pct = float(
                            str(live_pchange).replace("%", "").replace(",", "")
                        )
                    elif (
                        underlying_price is not None
                        and previous_close is not None
                        and previous_close > 0
                    ):
                        price_change_pct = (
                            (underlying_price / previous_close - 1) * 100
                        )

                    if underlying_price is not None and price_change_pct is not None:
                        live_quote_ok = True
                        break

                except Exception:
                    if quote_attempt < 2:
                        time.sleep(1.5)

            # --------------------------------------------------------
            # HISTORICAL FALLBACK
            # --------------------------------------------------------
            # If the live quote endpoint is temporarily unavailable,
            # use NSE historical security data to obtain previous close
            # and calculate the change from the option-chain underlying.
            if not live_quote_ok:
                try:
                    from datetime import date, timedelta

                    today = date.today()
                    from_date = today - timedelta(days=7)

                    hist_headers = {
                        "Referer": (
                            "https://www.nseindia.com/get-quotes/equity"
                            f"?symbol={symbol}"
                        ),
                        "Accept": "application/json,text/plain,*/*",
                        "X-Requested-With": "XMLHttpRequest",
                    }
                    session.headers.update(hist_headers)

                    hist_response = session.get(
                        "https://www.nseindia.com/api/historicalOR/"
                        "generateSecurityWiseHistoricalData",
                        params={
                            "symbol": symbol,
                            "from": from_date.strftime("%d-%m-%Y"),
                            "to": today.strftime("%d-%m-%Y"),
                            "series": "EQ",
                            "type": "priceVolumeDeliverable",
                            "csv": "false",
                        },
                        timeout=25
                    )
                    hist_response.raise_for_status()
                    hist_payload = hist_response.json()

                    hist_rows = (
                        hist_payload.get("data")
                        if isinstance(hist_payload, dict)
                        else hist_payload
                    ) or []

                    if hist_rows:
                        def row_date(row):
                            value = (
                                row.get("CH_TIMESTAMP")
                                or row.get("mTIMESTAMP")
                                or row.get("date")
                                or row.get("Date")
                                or ""
                            )
                            try:
                                return pd.to_datetime(value, dayfirst=True)
                            except Exception:
                                return pd.Timestamp.min

                        hist_rows = sorted(hist_rows, key=row_date)
                        latest_row = hist_rows[-1]

                        prev_value = (
                            latest_row.get("CH_PREV_CLS_PRICE")
                            or latest_row.get("chPreviousClsPrice")
                            or latest_row.get("Prev Close")
                            or latest_row.get("prevClose")
                        )

                        if prev_value is not None:
                            previous_close = float(
                                str(prev_value).replace(",", "")
                            )

                    if (
                        underlying_price is not None
                        and previous_close is not None
                        and previous_close > 0
                    ):
                        price_change_pct = (
                            (float(underlying_price) / previous_close - 1) * 100
                        )

                except Exception:
                    pass

            # Second NSE historical endpoint fallback.
            if price_change_pct is None:
                try:
                    from datetime import date, timedelta

                    today = date.today()
                    from_date = today - timedelta(days=7)

                    fallback_response = session.get(
                        "https://www.nseindia.com/api/historical/securityArchives",
                        params={
                            "symbol": symbol,
                            "from": from_date.strftime("%d-%m-%Y"),
                            "to": today.strftime("%d-%m-%Y"),
                            "series": "EQ",
                        },
                        timeout=25
                    )
                    fallback_response.raise_for_status()
                    fallback_payload = fallback_response.json()

                    rows = (
                        fallback_payload.get("data", [])
                        if isinstance(fallback_payload, dict)
                        else []
                    )

                    if rows:
                        latest = rows[-1]
                        prev_value = (
                            latest.get("CH_PREV_CLS_PRICE")
                            or latest.get("chPreviousClsPrice")
                            or latest.get("Prev Close")
                            or latest.get("prevClose")
                        )

                        if prev_value is not None:
                            previous_close = float(
                                str(prev_value).replace(",", "")
                            )

                        if (
                            underlying_price is not None
                            and previous_close is not None
                            and previous_close > 0
                        ):
                            price_change_pct = (
                                (float(underlying_price) / previous_close - 1)
                                * 100
                            )
                except Exception:
                    pass

            timestamp = (
                records.get("timestamp")
                or payload.get("timestamp")
                or ""
            )

            return (
                df.sort_values("Strike").reset_index(drop=True),
                expiry,
                timestamp,
                underlying_price,
                previous_close,
                price_change_pct
            )

        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(2)

    raise RuntimeError(
        f"NSE option-chain request failed after 4 attempts: {last_error}"
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
    selected_symbol = st.selectbox(
        "Search Stock",
        options=fno_stocks,
        index=None,
        placeholder="Type 1–2 letters and select a stock",
        key="stock_search"
    )

with col2:
    refresh = st.button(
        "🔄 Refresh",
        use_container_width=True
    )

if refresh:
    st.cache_data.clear()
    st.rerun()

if not selected_symbol:
    st.info(
        "Type 1–2 letters in the search box and select a stock."
    )
    st.stop()


# ============================================================
# EXPIRY
# ============================================================

try:
    expiry_dates = get_contract_info(
        create_nse_session(),
        selected_symbol
    )
except Exception as e:
    st.error(f"Unable to load expiry dates: {e}")
    st.stop()

expiry = st.selectbox(
    "Expiry",
    options=expiry_dates,
    index=0,
    key=f"expiry_{selected_symbol}"
)

with st.spinner(
    f"Fetching NSE option-chain data for "
    f"{selected_symbol} ({expiry})..."
):
    try:
        (
            df,
            expiry,
            nse_timestamp,
            underlying_price,
            previous_close,
            price_change_pct
        ) = get_option_chain(selected_symbol, expiry)
    except Exception as e:
        st.error(f"Unable to fetch NSE option-chain data: {e}")
        st.info(
            "The app automatically retries NSE requests. "
            "If NSE is temporarily unavailable, press Refresh."
        )
        st.stop()


if nse_timestamp:
    last_updated = str(nse_timestamp)
else:
    last_updated = datetime.now(
        ZoneInfo("Asia/Kolkata")
    ).strftime("%d-%b-%Y %H:%M:%S IST")


st.markdown(f"### {selected_symbol}")

c1, c2, c3, c4 = st.columns(4)

with c1:
    st.metric(
        "Underlying Price",
        f"₹{underlying_price:,.2f}" if underlying_price is not None else "N/A",
        f"{price_change_pct:+.2f}%" if price_change_pct is not None else None
    )
with c2:
    st.metric("Expiry", expiry)
with c3:
    st.metric("Last Updated", last_updated)
with c4:
    st.metric("Strikes Analysed", f"{len(df):,}")

st.divider()

# ============================================================
# QUICK OI ANALYSIS
# ============================================================

total_ce_oi = float(df["CE OI"].sum())
total_pe_oi = float(df["PE OI"].sum())
pcr = total_pe_oi / total_ce_oi if total_ce_oi > 0 else None

pain = {}
for settlement in df["Strike"]:
    call_pain = (df["CE OI"] * (settlement - df["Strike"]).clip(lower=0)).sum()
    put_pain = (df["PE OI"] * (df["Strike"] - settlement).clip(lower=0)).sum()
    pain[settlement] = call_pain + put_pain

max_pain = min(pain, key=pain.get) if pain else None
ce_max = df.loc[df["CE OI"].idxmax(), "Strike"]
pe_max = df.loc[df["PE OI"].idxmax(), "Strike"]

st.subheader("🔎 Quick OI Analysis")

m1, m2, m3, m4 = st.columns(4)
with m1:
    st.metric("PCR (OI)", f"{pcr:.2f}" if pcr is not None else "N/A")
with m2:
    st.metric("Max Pain", f"{max_pain:,.0f}" if max_pain is not None else "N/A")
with m3:
    st.metric("Highest CE OI", f"{ce_max:,.0f}")
with m4:
    st.metric("Highest PE OI", f"{pe_max:,.0f}")

if underlying_price is not None:
    ce_view = "Potential resistance" if ce_max > underlying_price else "At/below price"
    pe_view = "Potential support" if pe_max < underlying_price else "At/above price"
    st.caption(
        f"Highest CE OI: {ce_max:,.0f} — {ce_view}  •  "
        f"Highest PE OI: {pe_max:,.0f} — {pe_view}"
    )

ce_add = float(df["CE Change OI"].clip(lower=0).sum())
pe_add = float(df["PE Change OI"].clip(lower=0).sum())
ce_unwind = float((-df["CE Change OI"].clip(upper=0)).sum())
pe_unwind = float((-df["PE Change OI"].clip(upper=0)).sum())

# Simple price + total OI interpretation
total_oi_change = float(df["CE Change OI"].sum() + df["PE Change OI"].sum())

if price_change_pct is not None and price_change_pct > 0 and total_oi_change > 0:
    interpretation = "🟢 Long Buildup"
elif price_change_pct is not None and price_change_pct < 0 and total_oi_change > 0:
    interpretation = "🔴 Short Buildup"
elif price_change_pct is not None and price_change_pct > 0 and total_oi_change < 0:
    interpretation = "🔵 Short Covering"
elif price_change_pct is not None and price_change_pct < 0 and total_oi_change < 0:
    interpretation = "🟠 Long Unwinding"
else:
    interpretation = "⚪ No Clear Setup"

st.subheader("🔎 OI Interpretation")
if price_change_pct is not None:
    st.info(
        f"{interpretation}  •  Price: {price_change_pct:+.2f}%  •  "
        f"OI: {total_oi_change:+,.0f}"
    )
else:
    st.warning(
        "Live price change could not be obtained from NSE — OI interpretation cannot be classified."
    )

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
