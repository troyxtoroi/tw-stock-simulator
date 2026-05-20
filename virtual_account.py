"""
虛擬帳戶模組 (Virtual Account Module)
=======================================
功能：
  1. SQLite 本地資料庫（帳戶餘額表、持倉表、交易紀錄表）
  2. 台股費用計算（手續費 + 證交稅）
  3. 買進 / 賣出 指令執行（自動扣費、寫入 DB）
  4. 帳戶損益報表查詢

台股費用規則：
  手續費：成交金額 × 0.1425%（最低 NT$20），買賣皆收
  證交稅：成交金額 × 0.3%（一般股票）/ 0.1%（ETF），僅賣出收取
  ※ 券商通常提供手續費折扣，預設折數 0.6（即 0.0855%）

初始設定：
  虛擬本金   NT$100,000
  收益目標   NT$5,000,000

依賴套件：
  pip install pandas  （sqlite3 為 Python 內建，無需安裝）
"""

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd


# ── 全域常數 ───────────────────────────────────────────────────────────────────

INITIAL_CAPITAL: float = 100_000.0    # 虛擬本金（台幣）
PROFIT_TARGET:   float = 5_000_000.0  # 收益目標（台幣）

# 手續費基準費率（買賣皆收）
COMMISSION_RATE: float = 0.001425     # 0.1425%
COMMISSION_MIN:  float = 20.0         # 最低手續費 NT$20

# 證交稅費率（僅賣出收取）
STT_STOCK_RATE: float = 0.003         # 一般股票 0.3%
STT_ETF_RATE:   float = 0.001         # ETF 0.1%

# 預設資料庫路徑
DEFAULT_DB_PATH: str = "tw_stock_sim.db"

# 台灣主流 ETF 代號集合（用於判斷適用 0.1% 證交稅）
TAIWAN_ETF_PREFIXES = ("00", "0050", "0051", "0052", "0053", "0054",
                       "0055", "0056", "0057", "0058", "0059")


# ── 工具函式 ───────────────────────────────────────────────────────────────────

def _is_etf(ticker: str) -> bool:
    """判斷是否為 ETF（台灣 ETF 代號通常以 '00' 開頭，或為 0050 系列）。"""
    return ticker.startswith("00") or ticker in ("0050", "0051", "0052")


def _now() -> str:
    """回傳 ISO 8601 格式的當前時間字串。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── 費用計算器 ─────────────────────────────────────────────────────────────────

@dataclass
class FeeResult:
    """費用計算結果的資料容器。"""
    trade_amount:  float   # 成交金額（股數 × 單價）
    commission:    float   # 手續費
    stt:           float   # 證交稅（賣出才有；買進為 0）
    total_fee:     float   # 手續費 + 證交稅
    net_amount:    float   # 實際付出（買）或實際收入（賣）


def calculate_fees(
    ticker:            str,
    shares:            int,
    price:             float,
    trade_type:        str,          # "BUY" 或 "SELL"
    commission_discount: float = 0.6,  # 券商折數，0.6 = 六折
) -> FeeResult:
    """
    計算台股單筆交易的手續費與證交稅。

    Parameters
    ----------
    ticker              : 股票代號
    shares              : 交易股數（支援零股）
    price               : 成交單價（元）
    trade_type          : "BUY"（買進）或 "SELL"（賣出）
    commission_discount : 手續費折數（0.0–1.0），預設 0.6（六折）

    Returns
    -------
    FeeResult dataclass

    範例
    ----
    >>> fee = calculate_fees("0050", 1000, 185.0, "BUY")
    >>> print(fee.net_amount)   # 185,000 + 手續費
    """
    trade_amount = shares * price

    # ── 手續費（買賣皆收）──────────────────────────────────────────────────
    raw_commission = trade_amount * COMMISSION_RATE * commission_discount
    # 無條件捨去至整數元，但不低於最低手續費
    commission = max(int(raw_commission), COMMISSION_MIN)

    # ── 證交稅（僅賣出收取）───────────────────────────────────────────────
    if trade_type.upper() == "SELL":
        stt_rate = STT_ETF_RATE if _is_etf(ticker) else STT_STOCK_RATE
        stt = int(trade_amount * stt_rate)  # 無條件捨去至整數元
    else:
        stt = 0  # 買進不收證交稅

    total_fee = commission + stt

    # 買進：實際付出 = 成交金額 + 費用
    # 賣出：實際收入 = 成交金額 - 費用
    if trade_type.upper() == "BUY":
        net_amount = trade_amount + total_fee
    else:
        net_amount = trade_amount - total_fee

    return FeeResult(
        trade_amount=round(trade_amount, 2),
        commission=float(commission),
        stt=float(stt),
        total_fee=float(total_fee),
        net_amount=round(net_amount, 2),
    )


# ── 資料庫初始化 ───────────────────────────────────────────────────────────────

def init_database(db_path: str = DEFAULT_DB_PATH) -> None:
    """
    建立 SQLite 資料庫與三張資料表（若已存在則略過）。

    資料表設計：
      account_balance  — 帳戶餘額快照（每筆交易後更新）
      holdings         — 目前持倉（每支股票一列）
      trade_records    — 所有交易明細（只增不改）
    """
    conn = sqlite3.connect(db_path)
    cur  = conn.cursor()

    # ── 帳戶餘額表 ─────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS account_balance (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            cash            REAL    NOT NULL,   -- 可用現金
            total_invested  REAL    NOT NULL,   -- 累計投入本金
            realized_pnl    REAL    NOT NULL,   -- 已實現損益
            initial_capital REAL    NOT NULL,   -- 初始本金
            profit_target   REAL    NOT NULL,   -- 收益目標
            updated_at      TEXT    NOT NULL    -- 最後更新時間
        )
    """)

    # ── 持倉表 ─────────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS holdings (
            ticker      TEXT    PRIMARY KEY,    -- 股票代號
            shares      INTEGER NOT NULL,       -- 持有股數
            avg_cost    REAL    NOT NULL,       -- 平均買進成本（含手續費）
            total_cost  REAL    NOT NULL,       -- 總持倉成本
            updated_at  TEXT    NOT NULL
        )
    """)

    # ── 交易紀錄表 ─────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trade_records (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id       TEXT    UNIQUE NOT NULL,  -- UUID，唯一交易識別碼
            trade_time     TEXT    NOT NULL,          -- 交易時間 (ISO 8601)
            ticker         TEXT    NOT NULL,          -- 股票代號
            trade_type     TEXT    NOT NULL,          -- BUY / SELL
            shares         INTEGER NOT NULL,          -- 交易股數
            price          REAL    NOT NULL,          -- 成交單價
            trade_amount   REAL    NOT NULL,          -- 成交金額（股數×單價）
            commission     REAL    NOT NULL,          -- 手續費
            stt            REAL    NOT NULL,          -- 證交稅
            total_fee      REAL    NOT NULL,          -- 手續費 + 證交稅
            net_amount     REAL    NOT NULL,          -- 實際付出（買）/ 實際收入（賣）
            realized_pnl   REAL    NOT NULL DEFAULT 0,-- 本筆已實現損益（賣出才有）
            balance_before REAL    NOT NULL,          -- 交易前現金餘額
            balance_after  REAL    NOT NULL,          -- 交易後現金餘額
            notes          TEXT                       -- 備註（可為空）
        )
    """)

    conn.commit()
    conn.close()


# ── 虛擬帳戶主類別 ─────────────────────────────────────────────────────────────

class VirtualAccount:
    """
    台股模擬交易虛擬帳戶。

    Usage
    -----
    >>> acc = VirtualAccount()
    >>> acc.buy("0050", shares=1000, price=185.0)
    >>> acc.sell("0050", shares=500, price=190.0)
    >>> print(acc.summary())
    """

    def __init__(
        self,
        db_path:             str   = DEFAULT_DB_PATH,
        commission_discount: float = 0.6,
    ) -> None:
        """
        初始化帳戶。若資料庫不存在則自動建立並寫入初始餘額。

        Parameters
        ----------
        db_path             : SQLite 檔案路徑
        commission_discount : 手續費折數（預設六折）
        """
        self.db_path             = db_path
        self.commission_discount = commission_discount

        # 確保資料庫與資料表存在
        init_database(db_path)

        # 若帳戶餘額表為空，寫入初始狀態
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM account_balance").fetchone()
            if row[0] == 0:
                conn.execute("""
                    INSERT INTO account_balance
                        (cash, total_invested, realized_pnl,
                         initial_capital, profit_target, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (INITIAL_CAPITAL, 0.0, 0.0,
                      INITIAL_CAPITAL, PROFIT_TARGET, _now()))
                conn.commit()
                print(f"[INIT] 帳戶建立完成，初始本金 NT${INITIAL_CAPITAL:,.0f}，"
                      f"目標 NT${PROFIT_TARGET:,.0f}")

    # ── 內部工具 ───────────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        """取得啟用 WAL 模式的 SQLite 連線（減少寫入鎖定風險）。"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row   # 讓結果可用欄位名稱存取
        return conn

    def _get_balance(self, conn: sqlite3.Connection) -> sqlite3.Row:
        """讀取最新帳戶餘額列（取最後一列）。"""
        return conn.execute(
            "SELECT * FROM account_balance ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def _update_balance(
        self,
        conn:           sqlite3.Connection,
        cash_delta:     float,   # 正=增加現金，負=減少現金
        invested_delta: float,   # 正=增加投入本金
        pnl_delta:      float,   # 已實現損益變化
    ) -> tuple[float, float]:
        """
        更新帳戶餘額並插入新快照列。

        Returns
        -------
        (balance_before, balance_after)
        """
        old = self._get_balance(conn)
        balance_before = old["cash"]
        balance_after  = round(old["cash"] + cash_delta, 2)

        conn.execute("""
            INSERT INTO account_balance
                (cash, total_invested, realized_pnl,
                 initial_capital, profit_target, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            balance_after,
            round(old["total_invested"] + invested_delta, 2),
            round(old["realized_pnl"]   + pnl_delta,      2),
            old["initial_capital"],
            old["profit_target"],
            _now(),
        ))
        return balance_before, balance_after

    def _get_holding(
        self, conn: sqlite3.Connection, ticker: str
    ) -> Optional[sqlite3.Row]:
        """讀取指定股票的持倉資料；若無持倉則回傳 None。"""
        return conn.execute(
            "SELECT * FROM holdings WHERE ticker = ?", (ticker,)
        ).fetchone()

    # ── 核心交易方法 ───────────────────────────────────────────────────────

    def buy(
        self,
        ticker:  str,
        shares:  int,
        price:   float,
        notes:   str = "",
    ) -> dict:
        """
        執行買進指令。

        流程：
          1. 計算手續費（買進不收證交稅）
          2. 確認現金足夠
          3. 扣除現金，更新持倉平均成本
          4. 寫入交易紀錄表

        Parameters
        ----------
        ticker : 股票代號
        shares : 買進股數（1 張 = 1000 股；支援零股）
        price  : 買進單價（元）
        notes  : 備註

        Returns
        -------
        dict — 交易摘要（含費用明細）

        Raises
        ------
        ValueError : 餘額不足或參數不合法
        """
        if shares <= 0 or price <= 0:
            raise ValueError("股數與價格必須大於零。")

        fee = calculate_fees(ticker, shares, price, "BUY", self.commission_discount)

        with self._conn() as conn:
            bal = self._get_balance(conn)

            # 現金不足檢查
            if bal["cash"] < fee.net_amount:
                raise ValueError(
                    f"現金不足！需要 NT${fee.net_amount:,.2f}，"
                    f"現有 NT${bal['cash']:,.2f}"
                )

            # 更新帳戶餘額（現金減少、投入本金增加）
            balance_before, balance_after = self._update_balance(
                conn,
                cash_delta     = -fee.net_amount,
                invested_delta = +fee.net_amount,
                pnl_delta      = 0.0,
            )

            # 更新持倉（計算加權平均成本）
            holding = self._get_holding(conn, ticker)
            if holding:
                new_shares    = holding["shares"] + shares
                new_total_cost = holding["total_cost"] + fee.net_amount
                new_avg_cost  = new_total_cost / new_shares
                conn.execute("""
                    UPDATE holdings
                    SET shares = ?, avg_cost = ?, total_cost = ?, updated_at = ?
                    WHERE ticker = ?
                """, (new_shares, round(new_avg_cost, 4),
                      round(new_total_cost, 2), _now(), ticker))
            else:
                avg_cost = fee.net_amount / shares
                conn.execute("""
                    INSERT INTO holdings (ticker, shares, avg_cost, total_cost, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (ticker, shares, round(avg_cost, 4),
                      round(fee.net_amount, 2), _now()))

            # 寫入交易紀錄
            trade_id = str(uuid.uuid4())
            conn.execute("""
                INSERT INTO trade_records
                    (trade_id, trade_time, ticker, trade_type, shares, price,
                     trade_amount, commission, stt, total_fee, net_amount,
                     realized_pnl, balance_before, balance_after, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_id, _now(), ticker, "BUY", shares, price,
                fee.trade_amount, fee.commission, fee.stt, fee.total_fee,
                fee.net_amount, 0.0,
                balance_before, balance_after, notes,
            ))
            conn.commit()

        result = {
            "trade_id":     trade_id,
            "action":       "BUY",
            "ticker":       ticker,
            "shares":       shares,
            "price":        price,
            "trade_amount": fee.trade_amount,
            "commission":   fee.commission,
            "stt":          fee.stt,
            "total_fee":    fee.total_fee,
            "net_paid":     fee.net_amount,      # 實際付出
            "balance_before": balance_before,
            "balance_after":  balance_after,
        }
        print(
            f"[BUY]  {ticker:>6}  {shares:>6} 股  @{price:>8.2f}  "
            f"成交 {fee.trade_amount:>10,.0f}  手續費 {fee.commission:>5.0f}  "
            f"實付 {fee.net_amount:>10,.0f}  餘額 {balance_after:>12,.0f}"
        )
        return result

    def sell(
        self,
        ticker:  str,
        shares:  int,
        price:   float,
        notes:   str = "",
    ) -> dict:
        """
        執行賣出指令。

        流程：
          1. 確認持倉股數足夠
          2. 計算手續費 + 證交稅，計算已實現損益
          3. 增加現金，更新持倉
          4. 寫入交易紀錄表

        Parameters
        ----------
        ticker : 股票代號
        shares : 賣出股數
        price  : 賣出單價（元）
        notes  : 備註

        Returns
        -------
        dict — 交易摘要（含損益）

        Raises
        ------
        ValueError : 持股不足或參數不合法
        """
        if shares <= 0 or price <= 0:
            raise ValueError("股數與價格必須大於零。")

        fee = calculate_fees(ticker, shares, price, "SELL", self.commission_discount)

        with self._conn() as conn:
            holding = self._get_holding(conn, ticker)

            # 持股不足檢查
            if not holding or holding["shares"] < shares:
                held = holding["shares"] if holding else 0
                raise ValueError(
                    f"持股不足！欲賣 {shares} 股，現持 {held} 股（{ticker}）"
                )

            # 計算本筆已實現損益
            # 成本依持倉平均成本比例攤提
            cost_basis   = holding["avg_cost"] * shares   # 這批股票的帳面成本
            realized_pnl = round(fee.net_amount - cost_basis, 2)

            # 更新帳戶餘額（現金增加；已實現損益更新）
            balance_before, balance_after = self._update_balance(
                conn,
                cash_delta     = +fee.net_amount,
                invested_delta = 0.0,
                pnl_delta      = realized_pnl,
            )

            # 更新持倉
            remaining_shares = holding["shares"] - shares
            if remaining_shares == 0:
                conn.execute("DELETE FROM holdings WHERE ticker = ?", (ticker,))
            else:
                new_total_cost = holding["avg_cost"] * remaining_shares
                conn.execute("""
                    UPDATE holdings
                    SET shares = ?, total_cost = ?, updated_at = ?
                    WHERE ticker = ?
                """, (remaining_shares, round(new_total_cost, 2), _now(), ticker))

            # 寫入交易紀錄
            trade_id = str(uuid.uuid4())
            conn.execute("""
                INSERT INTO trade_records
                    (trade_id, trade_time, ticker, trade_type, shares, price,
                     trade_amount, commission, stt, total_fee, net_amount,
                     realized_pnl, balance_before, balance_after, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_id, _now(), ticker, "SELL", shares, price,
                fee.trade_amount, fee.commission, fee.stt, fee.total_fee,
                fee.net_amount, realized_pnl,
                balance_before, balance_after, notes,
            ))
            conn.commit()

        result = {
            "trade_id":      trade_id,
            "action":        "SELL",
            "ticker":        ticker,
            "shares":        shares,
            "price":         price,
            "trade_amount":  fee.trade_amount,
            "commission":    fee.commission,
            "stt":           fee.stt,
            "total_fee":     fee.total_fee,
            "net_received":  fee.net_amount,    # 實際收入
            "realized_pnl":  realized_pnl,
            "balance_before": balance_before,
            "balance_after":  balance_after,
        }
        pnl_sign = "+" if realized_pnl >= 0 else ""
        print(
            f"[SELL] {ticker:>6}  {shares:>6} 股  @{price:>8.2f}  "
            f"成交 {fee.trade_amount:>10,.0f}  手續費 {fee.commission:>4.0f}  "
            f"證交稅 {fee.stt:>5.0f}  "
            f"損益 {pnl_sign}{realized_pnl:>8,.0f}  餘額 {balance_after:>12,.0f}"
        )
        return result

    # ── 查詢方法 ───────────────────────────────────────────────────────────

    def get_balance(self) -> dict:
        """回傳目前帳戶現金餘額與基本資訊。"""
        with self._conn() as conn:
            row = self._get_balance(conn)
            return dict(row)

    def get_holdings(self) -> pd.DataFrame:
        """
        回傳目前所有持倉的 DataFrame。

        欄位：ticker, shares, avg_cost, total_cost, updated_at
        """
        with self._conn() as conn:
            df = pd.read_sql_query(
                "SELECT * FROM holdings ORDER BY ticker", conn
            )
        return df

    def get_trade_history(
        self,
        ticker:     Optional[str] = None,
        trade_type: Optional[str] = None,
        limit:      int           = 50,
    ) -> pd.DataFrame:
        """
        查詢交易紀錄。

        Parameters
        ----------
        ticker     : 篩選特定股票（None = 全部）
        trade_type : "BUY" / "SELL" / None（全部）
        limit      : 最多回傳筆數

        Returns
        -------
        pd.DataFrame（由新到舊排序）
        """
        conditions, params = [], []

        if ticker:
            conditions.append("ticker = ?")
            params.append(ticker.upper())
        if trade_type:
            conditions.append("trade_type = ?")
            params.append(trade_type.upper())

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)

        with self._conn() as conn:
            df = pd.read_sql_query(
                f"SELECT * FROM trade_records {where} "
                f"ORDER BY id DESC LIMIT ?",
                conn, params=params,
            )
        return df

    def summary(self, current_prices: Optional[dict] = None) -> dict:
        """
        輸出帳戶損益報表。

        Parameters
        ----------
        current_prices : {ticker: price} 字典，用於計算未實現損益。
                         若為 None，則略過未實現損益計算。

        Returns
        -------
        dict — 包含現金、持倉、損益等完整資訊
        """
        with self._conn() as conn:
            bal = self._get_balance(conn)
            holdings_df = pd.read_sql_query(
                "SELECT * FROM holdings ORDER BY ticker", conn
            )
            trade_count = conn.execute(
                "SELECT COUNT(*) FROM trade_records"
            ).fetchone()[0]

        cash            = bal["cash"]
        realized_pnl    = bal["realized_pnl"]
        initial_capital = bal["initial_capital"]
        profit_target   = bal["profit_target"]

        # 計算未實現損益（需傳入即時價格）
        unrealized_pnl   = 0.0
        holdings_value   = 0.0
        holdings_details = []

        for _, row in holdings_df.iterrows():
            detail = {
                "ticker":     row["ticker"],
                "shares":     row["shares"],
                "avg_cost":   row["avg_cost"],
                "total_cost": row["total_cost"],
            }
            if current_prices and row["ticker"] in current_prices:
                mkt_price     = current_prices[row["ticker"]]
                mkt_value     = mkt_price * row["shares"]
                unreal        = mkt_value - row["total_cost"]
                unreal_pct    = unreal / row["total_cost"] * 100 if row["total_cost"] else 0
                detail.update({
                    "current_price":  mkt_price,
                    "market_value":   round(mkt_value, 2),
                    "unrealized_pnl": round(unreal, 2),
                    "unrealized_pct": round(unreal_pct, 2),
                })
                holdings_value += mkt_value
                unrealized_pnl += unreal
            else:
                holdings_value += row["total_cost"]  # 無即時價格時以成本替代

            holdings_details.append(detail)

        total_assets  = round(cash + holdings_value, 2)
        total_pnl     = round(realized_pnl + unrealized_pnl, 2)
        pnl_pct       = total_pnl / initial_capital * 100 if initial_capital else 0
        target_pct    = total_pnl / (profit_target - initial_capital) * 100 \
                        if (profit_target - initial_capital) else 0

        report = {
            "updated_at":      _now(),
            "initial_capital": initial_capital,
            "profit_target":   profit_target,
            "cash":            round(cash, 2),
            "holdings_value":  round(holdings_value, 2),
            "total_assets":    total_assets,
            "realized_pnl":    round(realized_pnl, 2),
            "unrealized_pnl":  round(unrealized_pnl, 2),
            "total_pnl":       total_pnl,
            "pnl_pct":         round(pnl_pct, 2),
            "target_progress": round(target_pct, 2),  # 距收益目標達成率 %
            "trade_count":     trade_count,
            "holdings":        holdings_details,
        }

        # 印出格式化報表
        sep = "─" * 52
        print(f"\n{'═'*52}")
        print(f"  虛擬帳戶報表  {report['updated_at']}")
        print(f"{'═'*52}")
        print(f"  初始本金   NT${initial_capital:>15,.0f}")
        print(f"  現金餘額   NT${cash:>15,.2f}")
        print(f"  持倉市值   NT${holdings_value:>15,.2f}")
        print(f"  總資產     NT${total_assets:>15,.2f}")
        print(sep)
        print(f"  已實現損益 NT${realized_pnl:>+15,.2f}")
        print(f"  未實現損益 NT${unrealized_pnl:>+15,.2f}")
        print(f"  總損益     NT${total_pnl:>+15,.2f}  ({pnl_pct:+.2f}%)")
        print(f"  目標進度       {target_pct:>14.2f}%  (目標 NT${profit_target:,.0f})")
        print(sep)
        print(f"  交易次數   {trade_count:>18} 筆")
        if holdings_details:
            print(f"\n  持倉明細：")
            for h in holdings_details:
                line = f"    {h['ticker']:>6}  {h['shares']:>7,} 股  成本 {h['avg_cost']:>8.2f}"
                if "market_value" in h:
                    line += (f"  市值 {h['market_value']:>10,.0f}"
                             f"  損益 {h['unrealized_pnl']:>+9,.0f}"
                             f" ({h['unrealized_pct']:+.2f}%)")
                print(line)
        print(f"{'═'*52}\n")

        return report


# ── 主程式示範 ────────────────────────────────────────────────────────────────

if __name__ == "__main__":

    import os
    # 每次執行清空舊資料庫，方便重複測試
    if os.path.exists(DEFAULT_DB_PATH):
        os.remove(DEFAULT_DB_PATH)

    acc = VirtualAccount(commission_discount=0.6)  # 六折手續費

    print("\n【買進測試】")
    acc.buy("0050",  shares=1000, price=185.50, notes="建倉 0050")
    acc.buy("00940", shares=3000, price=15.20,  notes="建倉 00940")
    acc.buy("2330",  shares=100,  price=920.00, notes="建倉 台積電零股")

    print("\n【賣出測試（獲利了結）】")
    acc.sell("0050",  shares=500,  price=192.00, notes="部分獲利了結")
    acc.sell("00940", shares=1000, price=16.50,  notes="00940 獲利")

    print("\n【賣出測試（停損）】")
    acc.sell("2330", shares=50, price=890.00, notes="停損")

    print("\n【帳戶報表（含模擬即時價格）】")
    current_prices = {"0050": 195.0, "00940": 16.80, "2330": 900.0}
    report = acc.summary(current_prices=current_prices)

    print("\n【交易紀錄（最近 10 筆）】")
    history = acc.get_trade_history(limit=10)
    cols = ["trade_time", "ticker", "trade_type", "shares",
            "price", "commission", "stt", "net_amount", "realized_pnl"]
    print(history[cols].to_string(index=False))

    print("\n【費用計算示範】")
    fee_buy  = calculate_fees("0050",  1000, 185.50, "BUY",  commission_discount=0.6)
    fee_sell = calculate_fees("00940", 3000,  16.50, "SELL", commission_discount=0.6)
    print(f"買 0050  1000股 @185.5 → 手續費 {fee_buy.commission:.0f} 元，"
          f"實付 {fee_buy.net_amount:,.0f} 元")
    print(f"賣 00940 3000股 @16.5  → 手續費 {fee_sell.commission:.0f} 元，"
          f"證交稅 {fee_sell.stt:.0f} 元，實收 {fee_sell.net_amount:,.0f} 元")
