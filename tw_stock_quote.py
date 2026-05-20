"""
台股報價接收模組 (Taiwan Stock Quote Module)
=============================================
功能：
  1. 抓取歷史 K 線資料 (yfinance)
  2. 抓取即時報價 (TWSE OpenAPI + Yahoo Finance 備援)

依賴套件：
  pip install yfinance pandas requests
"""

import json
import time
from datetime import datetime
from typing import Optional, Union

import pandas as pd
import requests
import yfinance as yf


# ── 常數設定 ───────────────────────────────────────────────────────────────────

# TWSE 即時報價 API（交易時間內有效；非交易時間回傳最後成交資料）
TWSE_REALTIME_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"

# 模擬瀏覽器 User-Agent，避免被 TWSE 擋掉
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://mis.twse.com.tw/",
}


# ── 工具函式 ───────────────────────────────────────────────────────────────────

def _to_tw_ticker(ticker: str, market: str = "tse") -> str:
    """
    將純數字代號轉為 Yahoo Finance 格式（加 .TW / .TWO）。
    market: 'tse'（上市）→ .TW  |  'tpex'（上櫃）→ .TWO
    """
    if ticker.endswith((".TW", ".TWO")):
        return ticker
    suffix = ".TW" if market == "tse" else ".TWO"
    return f"{ticker}{suffix}"


def _clean_float(value) -> float:
    """TWSE API 回傳值常含 '-' 或空字串，統一轉為 0.0。"""
    try:
        return float(str(value).split("_")[0])  # 最佳五檔以 '_' 分隔，只取第一檔
    except (ValueError, TypeError):
        return 0.0


# ── 功能一：歷史 K 線資料 ──────────────────────────────────────────────────────

def get_historical_kline(
    ticker: str,
    period: str = "1y",
    interval: str = "1d",
    start: Optional[str] = None,
    end: Optional[str] = None,
    market: str = "tse",
) -> pd.DataFrame:
    """
    抓取指定台股的歷史 K 線資料。

    Parameters
    ----------
    ticker   : 股票代號，如 "0050"、"00940"、"2330"
    period   : 資料期間（start 未指定時才有效）
               可選：1d / 5d / 1mo / 3mo / 6mo / 1y / 2y / 5y / 10y / ytd / max
    interval : K 線週期
               分線：1m / 2m / 5m / 15m / 30m / 60m / 90m / 1h
               日線以上：1d / 5d / 1wk / 1mo / 3mo
               ※ 分線資料最多保留 60 天
    start    : 起始日期，格式 "YYYY-MM-DD"（指定後 period 失效）
    end      : 結束日期，格式 "YYYY-MM-DD"（None 表示至今）
    market   : "tse"（上市）或 "tpex"（上櫃）

    Returns
    -------
    pd.DataFrame，欄位：
        ticker, date, open, high, low, close, adj_close, volume, source
    """
    yf_ticker = _to_tw_ticker(ticker, market)

    stock = yf.Ticker(yf_ticker)

    # 下載原始資料
    if start:
        raw = stock.history(start=start, end=end, interval=interval, auto_adjust=True)
    else:
        raw = stock.history(period=period, interval=interval, auto_adjust=True)

    # 若上市找不到，自動嘗試上櫃
    if raw.empty and market == "tse":
        print(f"[INFO] {yf_ticker} 無資料，改嘗試上櫃 (.TWO)…")
        return get_historical_kline(ticker, period, interval, start, end, market="tpex")

    if raw.empty:
        print(f"[WARN] {ticker} 查無歷史資料，請確認代號與市場類別。")
        return pd.DataFrame()

    # ── 整理成統一格式 ──────────────────────────────────────────────────────
    df = raw.reset_index()

    # yfinance 日線索引為 date；分線索引為 datetime（含時區）
    date_col = "Date" if "Date" in df.columns else "Datetime"
    df = df.rename(columns={date_col: "date"})

    # 移除時區資訊，保留純日期時間字串
    if pd.api.types.is_datetime64tz_dtype(df["date"]):
        df["date"] = df["date"].dt.tz_convert("Asia/Taipei").dt.tz_localize(None)

    # 統一欄位名稱（全部小寫）
    df.columns = [c.lower().replace(" ", "_") for c in df.columns]

    # 選取需要的欄位；adj_close 在 auto_adjust=True 時即為 close，保留以備查
    keep = ["date", "open", "high", "low", "close", "volume"]
    df = df[[c for c in keep if c in df.columns]].copy()

    # 加入識別欄位
    df.insert(0, "ticker", ticker)
    df["source"] = "yfinance"

    # 確保數值型別正確
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").round(2)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype(int)

    print(
        f"[OK] {ticker} 歷史資料：{len(df)} 筆，"
        f"{df['date'].iloc[0]} ～ {df['date'].iloc[-1]}"
    )
    return df


# ── 功能二-A：TWSE OpenAPI 即時報價（主要來源）────────────────────────────────

def get_realtime_twse(tickers: list[str], market: str = "tse") -> pd.DataFrame:
    """
    透過台灣證券交易所 OpenAPI 抓取即時報價。

    僅在「交易時間內」（09:00–13:30 平日）才能取得真正即時資料；
    盤後會回傳當日最後成交資訊。

    Parameters
    ----------
    tickers : 股票代號列表，如 ["0050", "2330", "00940"]
    market  : "tse"（上市）或 "tpex"（上櫃）

    Returns
    -------
    pd.DataFrame，欄位：
        ticker, name, date, time, open, high, low, close,
        prev_close, change, change_pct, volume, buy_price, sell_price, source
    """
    # TWSE 格式：tse_0050.tw 或 otc_00940.tw（上櫃用 otc_）
    prefix = "tse" if market == "tse" else "otc"
    ex_ch = "|".join(f"{prefix}_{t}.tw" for t in tickers)

    params = {"ex_ch": ex_ch, "json": "1", "delay": "0"}

    try:
        resp = requests.get(
            TWSE_REALTIME_URL,
            params=params,
            headers=REQUEST_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as e:
        print(f"[ERROR] TWSE API 請求失敗：{e}")
        return pd.DataFrame()

    msg_array = payload.get("msgArray", [])
    if not msg_array:
        print("[WARN] TWSE API 回傳空資料（可能非交易時間或代號有誤）。")
        return pd.DataFrame()

    records = []
    for item in msg_array:
        close_price = _clean_float(item.get("z"))  # 最新成交價（盤中）
        prev_close  = _clean_float(item.get("y"))  # 昨收價

        # 若盤中尚無成交，以參考價（昨收）代替
        if close_price == 0:
            close_price = prev_close

        change     = round(close_price - prev_close, 2) if prev_close else 0.0
        change_pct = round(change / prev_close * 100, 2) if prev_close else 0.0

        records.append({
            "ticker":     item.get("c", ""),          # 股票代號
            "name":       item.get("n", ""),          # 股票名稱
            "date":       item.get("d", ""),          # 日期 (YYYYMMDD)
            "time":       item.get("t", ""),          # 時間 (HH:MM:SS)
            "open":       _clean_float(item.get("o")),# 今日開盤
            "high":       _clean_float(item.get("h")),# 今日最高
            "low":        _clean_float(item.get("l")),# 今日最低
            "close":      close_price,                # 最新成交 / 參考價
            "prev_close": prev_close,                 # 昨收
            "change":     change,                     # 漲跌
            "change_pct": change_pct,                 # 漲跌幅 %
            "volume":     int(_clean_float(item.get("v", 0))),  # 成交量（張）
            "buy_price":  _clean_float(item.get("b", "0")),     # 最佳買價
            "sell_price": _clean_float(item.get("a", "0")),     # 最佳賣價
            "source":     "TWSE",
        })

    df = pd.DataFrame(records)
    print(f"[OK] TWSE 即時報價：{len(df)} 筆")
    return df


# ── 功能二-B：Yahoo Finance 即時報價（備援來源）──────────────────────────────

def get_realtime_yfinance(tickers: list[str], market: str = "tse") -> pd.DataFrame:
    """
    透過 Yahoo Finance 抓取即時報價（延遲約 15 分鐘）。

    TWSE API 失敗或非交易時間時可使用此函式作為備援。

    Parameters
    ----------
    tickers : 股票代號列表
    market  : "tse" 或 "tpex"

    Returns
    -------
    pd.DataFrame，欄位同 get_realtime_twse()
    """
    records = []

    for t in tickers:
        yf_ticker = _to_tw_ticker(t, market)
        try:
            stock = yf.Ticker(yf_ticker)
            fi    = stock.fast_info       # fast_info 速度較快，包含即時欄位
            info  = stock.info            # info 包含名稱等靜態資料（較慢）

            last_price  = fi.last_price or 0.0
            prev_close  = fi.previous_close or 0.0
            change      = round(last_price - prev_close, 2)
            change_pct  = round(change / prev_close * 100, 2) if prev_close else 0.0

            records.append({
                "ticker":     t,
                "name":       info.get("longName") or info.get("shortName", t),
                "date":       datetime.now().strftime("%Y%m%d"),
                "time":       datetime.now().strftime("%H:%M:%S"),
                "open":       round(fi.open or 0.0, 2),
                "high":       round(fi.day_high or 0.0, 2),
                "low":        round(fi.day_low or 0.0, 2),
                "close":      round(last_price, 2),
                "prev_close": round(prev_close, 2),
                "change":     change,
                "change_pct": change_pct,
                "volume":     int(fi.last_volume or 0),
                "buy":        None,      # Yahoo Finance 不提供買賣五檔
                "sell":       None,
                "source":     "Yahoo Finance",
            })

        except Exception as e:
            print(f"[WARN] Yahoo Finance 取得 {t} 失敗：{e}")
            continue

    df = pd.DataFrame(records)
    print(f"[OK] Yahoo Finance 即時報價：{len(df)} 筆")
    return df


# ── 整合介面：自動選擇最佳來源 ────────────────────────────────────────────────

def get_realtime_quote(
    tickers: list[str],
    market: str = "tse",
    source: str = "auto",
) -> pd.DataFrame:
    """
    取得即時報價的統一介面，自動處理 TWSE → Yahoo Finance 備援邏輯。

    Parameters
    ----------
    tickers : 股票代號列表
    market  : "tse"（上市）或 "tpex"（上櫃）
    source  : "auto"（優先 TWSE，失敗用 Yahoo）/ "twse" / "yfinance"

    Returns
    -------
    pd.DataFrame
    """
    if source == "twse":
        return get_realtime_twse(tickers, market)
    if source == "yfinance":
        return get_realtime_yfinance(tickers, market)

    # auto：先試 TWSE，失敗或空資料則改用 Yahoo Finance
    df = get_realtime_twse(tickers, market)
    if df.empty:
        print("[INFO] TWSE 無資料，切換至 Yahoo Finance…")
        df = get_realtime_yfinance(tickers, market)
    return df


# ── 格式轉換工具 ──────────────────────────────────────────────────────────────

def df_to_json(df: pd.DataFrame, indent: int = 2) -> str:
    """
    將 DataFrame 轉為格式化 JSON 字串（正確處理日期與 NaN）。

    Returns
    -------
    str — JSON 字串
    """
    return df.to_json(
        orient="records",
        force_ascii=False,   # 保留中文（股票名稱）
        date_format="iso",
        default_handler=str, # 處理無法序列化的型別
        indent=indent,
    )


# ── 主程式示範 ────────────────────────────────────────────────────────────────

if __name__ == "__main__":

    WATCH_LIST = ["0050", "00940", "2330"]   # 監控清單

    print("=" * 60)
    print("【功能一】歷史 K 線資料")
    print("=" * 60)

    # 抓取 0050 近一年日線
    hist_df = get_historical_kline("0050", period="1y", interval="1d")
    if not hist_df.empty:
        print(hist_df.tail(5).to_string(index=False))
        print()

    # 抓取 00940 指定區間月線
    hist_00940 = get_historical_kline(
        "00940", start="2024-01-01", end="2025-01-01", interval="1mo"
    )
    if not hist_00940.empty:
        print(hist_00940.to_string(index=False))

    print()
    print("=" * 60)
    print("【功能二】即時報價（TWSE OpenAPI → Yahoo Finance 備援）")
    print("=" * 60)

    rt_df = get_realtime_quote(WATCH_LIST, market="tse", source="auto")
    if not rt_df.empty:
        print(rt_df.to_string(index=False))

        # 輸出為 JSON
        json_str = df_to_json(rt_df)
        print("\n── JSON 格式預覽 ──")
        print(json_str[:800], "..." if len(json_str) > 800 else "")

        # 儲存 JSON 檔案
        with open("realtime_quotes.json", "w", encoding="utf-8") as f:
            f.write(json_str)
        print("\n[OK] 即時報價已儲存至 realtime_quotes.json")
