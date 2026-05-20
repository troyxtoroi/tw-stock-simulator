"""
策略運算模組 (Strategy Engine)
================================
策略：均線交叉 (Moving Average Crossover)
  黃金交叉：短期均線從下方突破長期均線 → BUY
  死亡交叉：短期均線從上方跌破長期均線 → SELL

Order 物件欄位對照：
  ┌──────────────┬────────────────┬─────────────────┐
  │  本模組       │  國泰證券        │  元富證券         │
  ├──────────────┼────────────────┼─────────────────┤
  │  Action      │  BSFlag (B/S)  │  buySell (B/S)  │
  │  Symbol      │  StockNo       │  stockNo        │
  │  Price       │  Price         │  price          │
  │  Quantity    │  Qty (張)       │  qty (股)        │
  │  OrderType   │  OrderCond     │  orderCond      │
  │  PriceType   │  PriceType     │  priceType      │
  │  TradeType   │  TradeType     │  tradeType      │
  └──────────────┴────────────────┴─────────────────┘
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd


# ── 列舉常數 ───────────────────────────────────────────────────────────────────

class Action:
    BUY  = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

class OrderType:
    ROD = "ROD"   # Rest of Day：當日有效，收盤未成交自動取消
    IOC = "IOC"   # Immediate or Cancel：立即成交否則取消
    FOK = "FOK"   # Fill or Kill：全部成交否則取消

class PriceType:
    LMT = "LMT"   # 限價委託
    MKT = "MKT"   # 市價委託

class TradeType:
    CASH   = "CASH"    # 現股
    MARGIN = "MARGIN"  # 融資（信用交易買進）
    SHORT  = "SHORT"   # 融券（信用交易賣出）


# ── 標準化訂單物件 ─────────────────────────────────────────────────────────────

@dataclass
class Order:
    """
    標準化訂單物件，格式對齊國泰 / 元富證券 API。

    欄位說明
    --------
    Action     : "BUY" 或 "SELL"
    Symbol     : 股票代號（不含 .TW 後綴）
    Price      : 委託價格；PriceType="MKT" 時填 0
    Quantity   : 委託股數（1 張 = 1000 股）
    OrderType  : "ROD" / "IOC" / "FOK"
    PriceType  : "LMT"（限價）/ "MKT"（市價）
    TradeType  : "CASH"（現股）/ "MARGIN"（融資）/ "SHORT"（融券）
    SignalTime : 策略觸發時間（ISO 8601）
    SignalReason : 人類可讀的觸發原因（如 "黃金交叉"）
    Strategy   : 策略名稱
    ShortMA    : 觸發當下短期均線數值
    LongMA     : 觸發當下長期均線數值
    """
    Action:       str
    Symbol:       str
    Price:        float
    Quantity:     int
    OrderType:    str   = OrderType.ROD
    PriceType:    str   = PriceType.LMT
    TradeType:    str   = TradeType.CASH
    SignalTime:   str   = field(default_factory=lambda: datetime.now().isoformat())
    SignalReason: str   = ""
    Strategy:     str   = ""
    ShortMA:      float = 0.0
    LongMA:       float = 0.0

    def to_dict(self) -> dict:
        """轉為普通 dict（可直接序列化為 JSON）。"""
        return asdict(self)

    def to_broker_cathay(self) -> dict:
        """轉為國泰證券 API 格式。"""
        return {
            "BSFlag":    "B" if self.Action == Action.BUY else "S",
            "StockNo":   self.Symbol,
            "Price":     self.Price,
            "Qty":       self.Quantity // 1000,   # 國泰以「張」為單位
            "TradeType": 0,                        # 0 = 現股
            "OrderCond": {"ROD": 0, "IOC": 1, "FOK": 2}.get(self.OrderType, 0),
            "PriceType": 0 if self.PriceType == PriceType.LMT else 1,
        }

    def to_broker_masterlink(self) -> dict:
        """轉為元富證券 API 格式。"""
        return {
            "buySell":   "B" if self.Action == Action.BUY else "S",
            "stockNo":   self.Symbol,
            "price":     self.Price,
            "qty":       self.Quantity,             # 元富以「股」為單位
            "tradeType": "Stock",
            "orderCond": {"ROD": "R", "IOC": "I", "FOK": "F"}.get(self.OrderType, "R"),
            "priceType": "L" if self.PriceType == PriceType.LMT else "M",
        }


# ── 策略訊號容器 ───────────────────────────────────────────────────────────────

@dataclass
class Signal:
    """單一策略訊號（內部使用，不直接暴露給外部）。"""
    date:        str    # 訊號日期
    action:      str    # Action.BUY / SELL
    close:       float  # 收盤價
    short_ma:    float  # 短期均線
    long_ma:     float  # 長期均線
    reason:      str    # 觸發原因描述


# ── 均線交叉策略 ───────────────────────────────────────────────────────────────

class MACrossStrategy:
    """
    均線交叉策略（Moving Average Crossover）。

    規則
    ----
    • 短期均線（預設 5 日）從下方**突破**長期均線（預設 20 日）→ 黃金交叉 → BUY
    • 短期均線從上方**跌破**長期均線 → 死亡交叉 → SELL
    • 兩線交叉後直到下次交叉前，維持該方向的訊號（避免重複觸發）

    Parameters
    ----------
    short_window  : 短期均線天數（預設 5）
    long_window   : 長期均線天數（預設 20）
    default_qty   : 預設每次訂單股數（預設 1000 股 = 1 張）
    order_type    : 預設委託類型（ROD / IOC / FOK）
    price_type    : 預設價格類型（LMT = 限價 / MKT = 市價）
    """

    NAME = "MA_CROSS"

    def __init__(
        self,
        short_window: int = 5,
        long_window:  int = 20,
        default_qty:  int = 1000,
        order_type:   str = OrderType.ROD,
        price_type:   str = PriceType.LMT,
    ) -> None:
        if short_window >= long_window:
            raise ValueError("short_window 必須小於 long_window。")
        self.short_window = short_window
        self.long_window  = long_window
        self.default_qty  = default_qty
        self.order_type   = order_type
        self.price_type   = price_type

    # ── 指標計算 ───────────────────────────────────────────────────────────

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        在 DataFrame 上計算均線並回傳附有新欄位的副本。

        輸入 df 需含欄位：date, close（來自 get_historical_kline）。

        新增欄位
        --------
        ma_short  : 短期簡單移動平均（SMA）
        ma_long   : 長期簡單移動平均（SMA）
        position  : 1（短線在長線之上）/ -1（短線在長線之下）/ 0（均線尚未就緒）
        """
        out = df.copy()
        out["ma_short"] = (
            out["close"]
            .rolling(window=self.short_window, min_periods=self.short_window)
            .mean()
            .round(4)
        )
        out["ma_long"] = (
            out["close"]
            .rolling(window=self.long_window, min_periods=self.long_window)
            .mean()
            .round(4)
        )
        # 相對位置：+1 = 短線在上，-1 = 短線在下，0 = 資料不足
        out["position"] = 0
        valid = out["ma_short"].notna() & out["ma_long"].notna()
        out.loc[valid, "position"] = (
            out.loc[valid, "ma_short"] > out.loc[valid, "ma_long"]
        ).astype(int) * 2 - 1   # True→1, False→-1

        return out

    # ── 訊號偵測 ───────────────────────────────────────────────────────────

    def detect_signals(self, df: pd.DataFrame) -> list[Signal]:
        """
        掃描含均線欄位的 DataFrame，回傳所有交叉訊號列表（由舊到新）。

        偵測邏輯
        --------
        當 position 從 -1 → +1：黃金交叉（BUY）
        當 position 從 +1 → -1：死亡交叉（SELL）
        """
        signals: list[Signal] = []

        # 只看有效行（均線都已計算）
        valid = df[df["position"] != 0].reset_index(drop=True)
        if len(valid) < 2:
            return signals

        for i in range(1, len(valid)):
            prev_pos = valid.iloc[i - 1]["position"]
            curr_pos = valid.iloc[i]["position"]

            if prev_pos == -1 and curr_pos == 1:
                action = Action.BUY
                reason = f"黃金交叉（MA{self.short_window} 突破 MA{self.long_window}）"
            elif prev_pos == 1 and curr_pos == -1:
                action = Action.SELL
                reason = f"死亡交叉（MA{self.short_window} 跌破 MA{self.long_window}）"
            else:
                continue

            row = valid.iloc[i]
            date_val = row["date"]
            date_str = (
                str(date_val)[:10]
                if not isinstance(date_val, str)
                else date_val[:10]
            )
            signals.append(Signal(
                date     = date_str,
                action   = action,
                close    = float(row["close"]),
                short_ma = float(row["ma_short"]),
                long_ma  = float(row["ma_long"]),
                reason   = reason,
            ))

        return signals

    # ── 訂單生成 ───────────────────────────────────────────────────────────

    def generate_order(
        self,
        signal:   Signal,
        ticker:   str,
        quantity: Optional[int] = None,
    ) -> Order:
        """
        將策略訊號轉換為標準化 Order 物件。

        Parameters
        ----------
        signal   : 訊號物件
        ticker   : 股票代號
        quantity : 委託股數；None 則使用策略預設值
        """
        qty = quantity if quantity is not None else self.default_qty

        # 限價單：以訊號當日收盤價為委託基準
        price = signal.close if self.price_type == PriceType.LMT else 0.0

        return Order(
            Action       = signal.action,
            Symbol       = ticker,
            Price        = round(price, 2),
            Quantity     = qty,
            OrderType    = self.order_type,
            PriceType    = self.price_type,
            TradeType    = TradeType.CASH,
            SignalTime   = signal.date,
            SignalReason = signal.reason,
            Strategy     = (
                f"{self.NAME}_"
                f"MA{self.short_window}x"
                f"MA{self.long_window}"
            ),
            ShortMA      = signal.short_ma,
            LongMA       = signal.long_ma,
        )

    # ── 主入口 ────────────────────────────────────────────────────────────

    def run(
        self,
        df:       pd.DataFrame,
        ticker:   str,
        quantity: Optional[int] = None,
    ) -> dict:
        """
        完整策略執行流程。

        Parameters
        ----------
        df       : 來自 get_historical_kline() 的 DataFrame
        ticker   : 股票代號
        quantity : 每次委託股數

        Returns
        -------
        dict，包含：
          ticker        : 股票代號
          short_window  : 短期均線天數
          long_window   : 長期均線天數
          chart_data    : 含 OHLCV + 均線的 list（供前端繪圖）
          signals       : 所有訊號列表
          latest_signal : 最新訊號（或 None）
          latest_order  : 最新訊號對應的 Order（或 None）
        """
        if df.empty:
            return {"error": f"No data for {ticker}"}

        enriched = self.calculate_indicators(df)
        signals  = self.detect_signals(enriched)

        latest_signal = signals[-1] if signals else None
        latest_order  = (
            self.generate_order(latest_signal, ticker, quantity)
            if latest_signal else None
        )

        # 序列化 chart_data（過濾 NaN → None）
        chart_rows = []
        for _, row in enriched.iterrows():
            date_val = row["date"]
            date_str = (
                str(date_val)[:10]
                if not isinstance(date_val, str)
                else date_val[:10]
            )
            chart_rows.append({
                "date":     date_str,
                "open":     _safe_float(row.get("open")),
                "high":     _safe_float(row.get("high")),
                "low":      _safe_float(row.get("low")),
                "close":    _safe_float(row.get("close")),
                "volume":   int(row["volume"]) if pd.notna(row.get("volume")) else 0,
                "ma_short": _safe_float(row.get("ma_short")),
                "ma_long":  _safe_float(row.get("ma_long")),
            })

        return {
            "ticker":        ticker,
            "short_window":  self.short_window,
            "long_window":   self.long_window,
            "chart_data":    chart_rows,
            "signals":       [
                {
                    "date":     s.date,
                    "action":   s.action,
                    "close":    s.close,
                    "short_ma": s.short_ma,
                    "long_ma":  s.long_ma,
                    "reason":   s.reason,
                }
                for s in signals
            ],
            "latest_signal": (
                {
                    "date":     latest_signal.date,
                    "action":   latest_signal.action,
                    "close":    latest_signal.close,
                    "short_ma": latest_signal.short_ma,
                    "long_ma":  latest_signal.long_ma,
                    "reason":   latest_signal.reason,
                }
                if latest_signal else None
            ),
            "latest_order": latest_order.to_dict() if latest_order else None,
        }


# ── 工具函式 ───────────────────────────────────────────────────────────────────

def _safe_float(val) -> Optional[float]:
    """將 NaN / None 轉為 None，其餘轉為 float（供 JSON 序列化）。"""
    if val is None:
        return None
    try:
        f = float(val)
        return None if math.isnan(f) else round(f, 4)
    except (TypeError, ValueError):
        return None


# ── 示範 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from tw_stock_quote import get_historical_kline

    df = get_historical_kline("0050", period="1y", interval="1d")

    strategy = MACrossStrategy(short_window=5, long_window=20, default_qty=1000)
    result   = strategy.run(df, ticker="0050")

    print(f"\n策略：{result['ticker']}  MA{result['short_window']} × MA{result['long_window']}")
    print(f"訊號數量：{len(result['signals'])}")
    print(f"\n所有訊號：")
    for s in result["signals"]:
        icon = "▲" if s["action"] == "BUY" else "▼"
        print(f"  {icon} {s['date']}  {s['action']:4}  "
              f"收盤 {s['close']:>8.2f}  "
              f"MA{strategy.short_window}={s['short_ma']:.2f}  "
              f"MA{strategy.long_window}={s['long_ma']:.2f}  "
              f"{s['reason']}")

    if result["latest_order"]:
        o = result["latest_order"]
        print(f"\n最新訂單（Order Object）：")
        print(f"  Action    : {o['Action']}")
        print(f"  Symbol    : {o['Symbol']}")
        print(f"  Price     : {o['Price']}")
        print(f"  Quantity  : {o['Quantity']} 股")
        print(f"  OrderType : {o['OrderType']}")
        print(f"  PriceType : {o['PriceType']}")
        print(f"  SignalTime: {o['SignalTime']}")
        print(f"  Reason    : {o['SignalReason']}")
