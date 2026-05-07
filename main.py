from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd
import twstock
import yfinance as yf

EMA_SPAN = 12
BOLLINGER_WINDOW = 21
BOLLINGER_STD = 2.1
DEFAULT_TOLERANCE_PCT = 1.0
TODAY = date.today()
THIS_YEAR_START = TODAY.replace(month=1, day=1)
LAST_YEAR_START = date(TODAY.year - 1, 1, 1)
LAST_YEAR_END = date(TODAY.year - 1, 12, 31)
MARKET_SORT_ORDER = {"興櫃": 0, "上櫃": 1, "上市": 2}
OFFICIAL_LIST_APIS = {
    "上市": "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
    "上櫃": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
    "興櫃": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_R",
}


@dataclass(frozen=True)
class TaiwanStock:
    code: str
    name: str
    market: str
    group: str
    yahoo_symbol: str


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必須是大於 0 的整數。")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必須是大於 0 的數值。")
    return parsed


def date_filter(series: pd.Series, start_date: date, end_date: date | None = None) -> pd.Series:
    filtered = series[series.index.date >= start_date]
    if end_date is not None:
        filtered = filtered[filtered.index.date <= end_date]
    return filtered.dropna()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "篩選去年整年或今年至今漲幅達門檻，且最新價格接近 12EMA、布林中軌或下軌的上市、上櫃、興櫃台股。"
        )
    )
    parser.add_argument(
        "--min-gain",
        type=positive_float,
        default=30.0,
        help="今年以來最低漲幅門檻，預設 30。",
    )
    parser.add_argument(
        "--max-tickers",
        type=positive_int,
        default=None,
        help="測試時可限制股票數量。",
    )
    parser.add_argument(
        "--chunk-size",
        type=positive_int,
        default=80,
        help="每批向 Yahoo 下載的股票數量，預設 80。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="自訂輸出 CSV 路徑。",
    )
    parser.add_argument(
        "--tolerance-pct",
        type=positive_float,
        default=DEFAULT_TOLERANCE_PCT,
        help="價格距離日線 12EMA、布林中軌或下軌的容忍百分比，預設 1。",
    )
    parser.add_argument(
        "--weekly-tolerance-pct",
        type=positive_float,
        default=3.0,
        help="價格距離週線 12EMA、布林中軌或下軌的容忍百分比，預設 3。",
    )
    parser.add_argument(
        "--monthly-tolerance-pct",
        type=positive_float,
        default=5.0,
        help="價格距離月線 12EMA、布林中軌或下軌的容忍百分比，預設 5。",
    )
    return parser.parse_args()


def yahoo_symbol_for_market(code: str, market: str) -> str:
    return f"{code}.TW" if market == "上市" else f"{code}.TWO"


def fetch_official_stock_list(market: str) -> list[dict[str, str]]:
    response = subprocess.run(
        ["curl", "-L", "--silent", "--max-time", "20", OFFICIAL_LIST_APIS[market]],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(response.stdout)
    if not isinstance(payload, list):
        raise ValueError(f"Unexpected payload for market {market}")
    return payload


def build_universe(max_tickers: int | None = None) -> list[TaiwanStock]:
    universe_by_code: dict[str, TaiwanStock] = {}
    for item in twstock.codes.values():
        if not item.code.isdigit() or len(item.code) != 4:
            continue
        if item.type != "股票":
            continue
        if item.market not in {"上市", "上櫃", "興櫃"}:
            continue

        universe_by_code[item.code] = TaiwanStock(
            code=item.code,
            name=item.name,
            market=item.market,
            group=item.group,
            yahoo_symbol=yahoo_symbol_for_market(item.code, item.market),
        )

    for market in ["上市", "上櫃", "興櫃"]:
        try:
            official_rows = fetch_official_stock_list(market)
        except Exception:
            continue

        for row in official_rows:
            code = str(
                row.get("公司代號")
                or row.get("SecuritiesCompanyCode")
                or ""
            ).strip()
            if not code.isdigit() or len(code) != 4:
                continue

            if code in universe_by_code:
                continue

            name = str(
                row.get("公司簡稱")
                or row.get("CompanyAbbreviation")
                or row.get("公司名稱")
                or row.get("CompanyName")
                or code
            ).strip()
            group = str(
                row.get("產業別")
                or row.get("SecuritiesIndustryCode")
                or ""
            ).strip()
            universe_by_code[code] = TaiwanStock(
                code=code,
                name=name,
                market=market,
                group=group,
                yahoo_symbol=yahoo_symbol_for_market(code, market),
            )

    universe = sorted(universe_by_code.values(), key=lambda stock: stock.code)
    if max_tickers is not None:
        return universe[:max_tickers]
    return universe


def chunked(items: Iterable[TaiwanStock], size: int) -> Iterable[list[TaiwanStock]]:
    chunk: list[TaiwanStock] = []
    for item in items:
        chunk.append(item)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def lookback_start() -> date:
    # 需要覆蓋去年整年、今年至今與近 21 個月布林計算（約 900 天）。
    return min(LAST_YEAR_START, TODAY - timedelta(days=900))


def normalize_download_frame(
    raw: pd.DataFrame,
    tickers: list[str],
) -> dict[str, pd.DataFrame]:
    histories: dict[str, pd.DataFrame] = {}
    if raw.empty:
        return histories

    if isinstance(raw.columns, pd.MultiIndex):
        first_level = set(raw.columns.get_level_values(0))
        second_level = set(raw.columns.get_level_values(1))

        for ticker in tickers:
            if ticker in first_level:
                frame = raw[ticker].copy()
            elif ticker in second_level:
                frame = raw.xs(ticker, axis=1, level=1).copy()
            else:
                continue

            frame = frame.dropna(how="all")
            if not frame.empty:
                histories[ticker] = frame
        return histories

    if len(tickers) == 1:
        frame = raw.copy().dropna(how="all")
        if not frame.empty:
            histories[tickers[0]] = frame
    return histories


def download_histories(
    stocks: list[TaiwanStock],
    chunk_size: int,
) -> dict[str, pd.DataFrame]:
    histories: dict[str, pd.DataFrame] = {}
    total_chunks = (len(stocks) + chunk_size - 1) // chunk_size

    for index, stock_chunk in enumerate(chunked(stocks, chunk_size), start=1):
        tickers = [stock.yahoo_symbol for stock in stock_chunk]
        print(f"[{index}/{total_chunks}] downloading {len(tickers)} tickers...")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            raw = yf.download(
                tickers=tickers,
                start=lookback_start().isoformat(),
                end=(TODAY + timedelta(days=1)).isoformat(),
                interval="1d",
                auto_adjust=True,
                group_by="ticker",
                progress=False,
                threads=True,
            )
        histories.update(normalize_download_frame(raw, tickers))

    return histories


def within_pct(value: float, target: float, tolerance_pct: float) -> bool:
    if target == 0:
        return False
    return abs(value - target) / abs(target) * 100.0 <= tolerance_pct


def evaluate_series(series: pd.Series, tolerance_pct: float, prefix: str) -> list[str]:
    if len(series) < BOLLINGER_WINDOW:
        return []

    ema12 = series.ewm(span=EMA_SPAN, adjust=False).mean()
    middle = series.rolling(window=BOLLINGER_WINDOW).mean()
    deviation = series.rolling(window=BOLLINGER_WINDOW).std(ddof=0)
    lower = middle - (BOLLINGER_STD * deviation)

    snapshot = pd.DataFrame(
        {
            "close": series,
            "ema12": ema12,
            "middle": middle,
            "lower": lower,
        }
    ).dropna()
    if snapshot.empty:
        return []

    latest = snapshot.iloc[-1]
    close_price = float(latest["close"])
    ema12 = float(latest["ema12"])
    middle = float(latest["middle"])
    lower = float(latest["lower"])
    
    matched_reasons: list[str] = []
    if within_pct(close_price, ema12, tolerance_pct):
        matched_reasons.append(f"{prefix}_near_ema12")
    if within_pct(close_price, middle, tolerance_pct):
        matched_reasons.append(f"{prefix}_near_middle")
    if within_pct(close_price, lower, tolerance_pct):
        matched_reasons.append(f"{prefix}_near_lower")

    return matched_reasons


def period_gain(close: pd.Series, start_date: date, end_date: date | None = None) -> float | None:
    period_data = date_filter(close, start_date, end_date)
    if period_data.empty:
        return None

    first_close = float(period_data.iloc[0])
    latest_close = float(period_data.iloc[-1])
    if first_close == 0:
        return None
    return ((latest_close / first_close) - 1.0) * 100.0


def screen_stocks(
    stocks: list[TaiwanStock],
    histories: dict[str, pd.DataFrame],
    min_gain: float,
    tolerance_pct: float,
    weekly_tolerance_pct: float,
    monthly_tolerance_pct: float,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []

    for stock in stocks:
        history = histories.get(stock.yahoo_symbol)
        if history is None or "Close" not in history.columns:
            continue

        close = history["Close"].dropna()
        if close.empty:
            continue

        this_year_gain = period_gain(close, THIS_YEAR_START)
        last_year_gain = period_gain(close, LAST_YEAR_START, LAST_YEAR_END)

        notes: list[str] = []
        if last_year_gain is not None and last_year_gain > 50:
            notes.append("去年漲幅超過50%")
        if this_year_gain is not None and this_year_gain > min_gain:
            gain_label = int(min_gain) if float(min_gain).is_integer() else min_gain
            notes.append(f"今年漲幅超過{gain_label}%")

        if not notes:
            continue

        matched_reasons = []
        
        # Daily
        matched_reasons.extend(evaluate_series(close, tolerance_pct, "daily"))
        
        # Weekly
        weekly_series = close.resample("W-FRI").last().dropna()
        if not weekly_series.empty:
            matched_reasons.extend(evaluate_series(weekly_series, weekly_tolerance_pct, "weekly"))
            
        # Monthly
        monthly_series = close.resample("ME").last().dropna()
        if not monthly_series.empty:
            matched_reasons.extend(evaluate_series(monthly_series, monthly_tolerance_pct, "monthly"))

        if not matched_reasons:
            continue

        result: dict[str, object] = {
            "code": stock.code,
            "name": stock.name,
            "market": stock.market,
            "latest_close": round(float(close.iloc[-1]), 2),
            "last_year_gain_pct": round(last_year_gain, 2) if last_year_gain is not None else "",
            "ytd_gain_pct": round(this_year_gain, 2) if this_year_gain is not None else "",
            "note": "；".join(notes),
            "matched_reason": ",".join(matched_reasons),
        }

        rows.append(result)

    table = pd.DataFrame(rows)
    if table.empty:
        return table

    table["market_sort_order"] = table["market"].map(MARKET_SORT_ORDER).fillna(len(MARKET_SORT_ORDER))
    return table.sort_values(
        by=["market_sort_order", "note", "ytd_gain_pct", "last_year_gain_pct", "code"],
        ascending=[True, False, False, False, True],
    ).drop(columns=["market_sort_order"]).reset_index(drop=True)


def build_export_table(result: pd.DataFrame) -> pd.DataFrame:
    if result.empty:
        return result

    export = result.loc[
        :,
        ["code", "name", "market", "latest_close", "last_year_gain_pct", "ytd_gain_pct", "note", "matched_reason"],
    ].copy()
    export["matched_reason"] = export["matched_reason"].apply(localize_matched_reason)
    export = export.rename(
        columns={
            "code": "股票代碼",
            "name": "股票名",
            "market": "市場別",
            "latest_close": "現價",
            "last_year_gain_pct": "去年漲幅(%)",
            "ytd_gain_pct": "今年漲幅(%)",
            "note": "註解",
            "matched_reason": "觸碰到哪個區塊",
        }
    )
    return export


def localize_matched_reason(value: str) -> str:
    reason_map = {
        "daily_near_ema12": "日線接近12EMA",
        "daily_near_middle": "日線接近中軌",
        "daily_near_lower": "日線接近下軌",
        "weekly_near_ema12": "週線接近12EMA",
        "weekly_near_middle": "週線接近中軌",
        "weekly_near_lower": "週線接近下軌",
        "monthly_near_ema12": "月線接近12EMA",
        "monthly_near_middle": "月線接近中軌",
        "monthly_near_lower": "月線接近下軌",
    }

    return "、".join(reason_map.get(reason, reason) for reason in value.split(",") if reason)


def save_output(result: pd.DataFrame, output_path: Path | None) -> Path:
    if output_path is None:
        output_dir = Path("outputs")
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = output_dir / f"taiwan_stock_screen_{timestamp}.csv"
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.suffix.lower() != ".csv":
            output_path = output_path.with_suffix(".csv")

    build_export_table(result).to_csv(output_path, index=False, encoding="utf-8-sig")
    return output_path


def main() -> None:
    args = parse_args()
    stocks = build_universe(max_tickers=args.max_tickers)
    print(f"Loaded {len(stocks)} Taiwan stocks from twstock.")

    histories = download_histories(stocks, chunk_size=args.chunk_size)
    result = screen_stocks(
        stocks=stocks,
        histories=histories,
        min_gain=args.min_gain,
        tolerance_pct=args.tolerance_pct,
        weekly_tolerance_pct=args.weekly_tolerance_pct,
        monthly_tolerance_pct=args.monthly_tolerance_pct,
    )

    if result.empty:
        print("No stocks matched the current filters.")
        return

    output_path = save_output(result, args.output)
    print(f"Matched {len(result)} stocks.")
    print(f"Saved CSV to {output_path.resolve()}")


if __name__ == "__main__":
    main()
