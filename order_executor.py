"""
訂單執行模組 v2 — IS_SIMULATION 雙模式架構
==========================================

環境變數控制執行模式：
  IS_SIMULATION=true  → SimulatedBroker（虛擬帳戶 + SQLite，開發/測試用）
  IS_SIMULATION=false → 真實券商 API（CathayBroker / MasterLinkBroker）

架構圖
------
  OrderExecutor
      │
      ├── IS_SIMULATION=true  → SimulatedBroker  → VirtualAccount（SQLite）
      │
      └── IS_SIMULATION=false → CathayBroker      → 富果 Fugle Trade API
                              → MasterLinkBroker  → 元富 MasterLink API
                              （依 BROKER 環境變數選擇）

設定方式（建議使用 .env 文件，詳見 .env.example）：
  IS_SIMULATION=true
  BROKER=cathay          # cathay | masterlink
  FUGLE_API_KEY=...
  FUGLE_CERT_PATH=...
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import asdict
from datetime import datetime
from typing import Optional

import requests

# ── dotenv 載入（開發環境用，生產環境直接設系統環境變數）──────────────────────
# pip install python-dotenv
try:
    from dotenv import load_dotenv
    load_dotenv()  # 讀取同目錄下的 .env 文件
except ImportError:
    pass  # 未安裝 python-dotenv 時，依賴系統環境變數

# ── 模式判斷 ───────────────────────────────────────────────────────────────────
# 從環境變數讀取；預設為 True（模擬模式），保護生產環境安全
IS_SIMULATION: bool = os.getenv("IS_SIMULATION", "true").strip().lower() in ("true", "1", "yes")
BROKER_NAME:   str  = os.getenv("BROKER", "cathay").strip().lower()

# ── 日誌 ───────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)
logger.info(f"訂單執行模式：{'🟡 模擬 (IS_SIMULATION=True)' if IS_SIMULATION else '🔴 實戰 (IS_SIMULATION=False)'}")

# ── 依賴 import ────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from strategy import Order, Action, OrderType, PriceType
from virtual_account import VirtualAccount

# ── Webhook 常數 ───────────────────────────────────────────────────────────────
MAX_RETRY       = 3
RETRY_DELAYS    = (2, 4, 8)
WEBHOOK_TIMEOUT = 8
LOG_KEEP        = 50
CONFIG_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webhook_config.json")

# ── 風險控管（從環境變數讀取，保護實戰帳戶）──────────────────────────────────
RISK_MAX_ORDER_AMOUNT = float(os.getenv("MAX_ORDER_AMOUNT", "500000"))  # 單筆最大金額（元）
RISK_MAX_SHARES       = int(os.getenv("MAX_SHARES",       "10000"))    # 單筆最大股數
RISK_MAX_DAILY_ORDERS = int(os.getenv("MAX_DAILY_ORDERS", "20"))       # 每日最大下單次數


# ══════════════════════════════════════════════════════════════════════════════
#  抽象介面：BrokerInterface
# ══════════════════════════════════════════════════════════════════════════════

class BrokerInterface(ABC):
    """
    券商介面基底類別（Abstract Base Class）。

    所有券商實作（模擬 / 國泰 / 元富）皆繼承此類別，
    確保 OrderExecutor 可以無縫切換，不需修改上層程式碼。

    子類別必須實作的方法：
      place_order  — 送出委託
      get_balance  — 查詢可用資金
      get_positions — 查詢持倉
      broker_name  — 券商名稱（property）
    """

    @property
    @abstractmethod
    def broker_name(self) -> str:
        """回傳券商名稱，用於日誌和 Webhook payload。"""

    @abstractmethod
    def place_order(self, order: Order) -> dict:
        """
        送出委託。

        Parameters
        ----------
        order : 標準化 Order 物件

        Returns
        -------
        dict，必含欄位：
          success       : bool
          trade_id      : str   唯一委託識別碼
          action        : str   "BUY" / "SELL"
          ticker        : str
          shares        : int
          price         : float
          trade_amount  : float
          commission    : float
          stt           : float
          total_fee     : float
          net_amount    : float  實際付出（買）/ 實際收入（賣）
          realized_pnl  : float  已實現損益（賣出才有，買進為 0）
          balance_before: float
          balance_after : float
          raw_response  : dict   券商原始回傳（模擬時為空 dict）
        """

    @abstractmethod
    def get_balance(self) -> dict:
        """
        查詢可用資金。

        Returns
        -------
        dict，必含：
          cash          : float  可用現金
          total_assets  : float  總資產
          realized_pnl  : float  已實現損益
        """

    @abstractmethod
    def get_positions(self) -> list[dict]:
        """
        查詢目前持倉。

        Returns
        -------
        list of dict，每筆含：
          ticker     : str
          shares     : int
          avg_cost   : float
          total_cost : float
        """


# ══════════════════════════════════════════════════════════════════════════════
#  模擬模式：SimulatedBroker
# ══════════════════════════════════════════════════════════════════════════════

class SimulatedBroker(BrokerInterface):
    """
    模擬券商：使用 VirtualAccount + SQLite，完整記錄所有虛擬交易。
    IS_SIMULATION=True 時啟用。
    """

    def __init__(self, account: VirtualAccount) -> None:
        self._account = account

    @property
    def broker_name(self) -> str:
        return "SimulatedBroker（虛擬帳戶）"

    def place_order(self, order: Order) -> dict:
        if order.Action == Action.BUY:
            result = self._account.buy(
                order.Symbol,
                shares = order.Quantity,
                price  = order.Price,
                notes  = f"[{order.Strategy}] {order.SignalReason}",
            )
        else:
            result = self._account.sell(
                order.Symbol,
                shares = order.Quantity,
                price  = order.Price,
                notes  = f"[{order.Strategy}] {order.SignalReason}",
            )
        # 補齊介面要求的 raw_response 欄位
        result.setdefault("raw_response", {})
        return result

    def get_balance(self) -> dict:
        snap = self._account.summary()
        return {
            "cash":         snap.get("cash", 0),
            "total_assets": snap.get("total_assets", 0),
            "realized_pnl": snap.get("realized_pnl", 0),
        }

    def get_positions(self) -> list[dict]:
        df = self._account.get_holdings()
        return df.to_dict(orient="records") if not df.empty else []


# ══════════════════════════════════════════════════════════════════════════════
#  實戰模式 - 國泰證券 / 富果 Fugle Trade API
# ══════════════════════════════════════════════════════════════════════════════

class CathayBroker(BrokerInterface):
    """
    國泰證券 × 富果 (Fugle Trade) 實戰介面。
    IS_SIMULATION=False 且 BROKER=cathay 時啟用。

    ── 安裝 SDK ──────────────────────────────────────────────────────────────
    pip install fugle-trade

    ── 必要環境變數（儲存於 .env 或系統環境，切勿寫死在程式碼中）────────────
    FUGLE_API_KEY      : 富果開放 API 金鑰（需至富果後台申請）
    FUGLE_API_SECRET   : API Secret（視 SDK 版本而異）
    FUGLE_CERT_PATH    : PKCS#12 憑證檔路徑，例如 /secrets/cert.pfx
                         ⚠ 生產環境請放在程式碼目錄「以外」的安全位置
    FUGLE_CERT_PASSWORD: 憑證密碼
                         ⚠ 生產環境建議使用 AWS Secrets Manager / Azure Key Vault
                           取得，不要明文存放在 .env 文件

    ── 憑證安全規範 ──────────────────────────────────────────────────────────
    1. 憑證文件（.pfx / .p12）加入 .gitignore，絕對不要 commit 進版本控制
    2. 憑證密碼使用以下任一方式儲存：
       a. 本地開發：.env 文件（加入 .gitignore）
       b. CI/CD：GitHub Actions Secrets / GitLab CI Variables
       c. 生產容器：Kubernetes Secret / Docker Swarm Secret
       d. 雲端服務：
          - AWS  → Secrets Manager + IAM Role（最推薦）
          - Azure → Key Vault + Managed Identity
          - GCP   → Secret Manager + Service Account
    3. 憑證有效期通常為 1 年，需在到期前 30 天至券商更換
    4. 建立憑證輪換 (rotation) 腳本並設定 Cron Job 提醒

    ── API Key 安全規範 ─────────────────────────────────────────────────────
    1. 最小權限原則：API Key 僅申請「下單」權限，不開啟「出金」等敏感功能
    2. IP 白名單：在富果後台設定允許的來源 IP，阻擋未授權存取
    3. Key 輪換：每 90 天更換一次 API Key
    4. 監控：設定異常交易告警（如單日超過 N 筆）
    5. 若 Key 外洩，立即至券商後台停用並重新申請
    """

    def __init__(self) -> None:
        # ── 從環境變數安全載入憑證資訊（切勿在此處硬編碼任何值）──────────────
        self._api_key       = self._require_env("FUGLE_API_KEY")
        self._cert_path     = self._require_env("FUGLE_CERT_PATH")
        self._cert_password = self._require_env("FUGLE_CERT_PASSWORD")
        self._sdk           = None  # 延遲初始化，避免 import 失敗阻塞整個模組

    @staticmethod
    def _require_env(key: str) -> str:
        """強制要求環境變數存在，否則拋出明確的 ConfigurationError。"""
        val = os.getenv(key, "").strip()
        if not val:
            raise EnvironmentError(
                f"實戰模式缺少必要環境變數：{key}\n"
                f"請在 .env 文件或系統環境變數中設定，參考 .env.example。"
            )
        return val

    def _get_sdk(self):
        """
        取得已初始化的 Fugle Trade SDK 實例（懶載入 + 連線池）。

        ── 真實實作步驟（取消下方註解）────────────────────────────────────
        from fugle_trade.sdk import SDK

        if self._sdk is None:
            # 使用憑證文件建立 SDK 實例
            # ⚠ 憑證路徑與密碼均來自環境變數，絕不硬編碼
            self._sdk = SDK(config_path=self._cert_path)
            self._sdk.login()
            logger.info("[Fugle] SDK 登入成功")

        return self._sdk
        ────────────────────────────────────────────────────────────────────
        """
        # ── 框架佔位（IS_SIMULATION=False 時需取消上方真實實作的註解）────────
        raise NotImplementedError(
            "CathayBroker._get_sdk() 尚未實作。\n"
            "請依照上方說明安裝 fugle-trade SDK 並取消相關程式碼的註解。"
        )

    @property
    def broker_name(self) -> str:
        return "CathayBroker（國泰 × 富果 Fugle Trade）"

    def place_order(self, order: Order) -> dict:
        """
        透過富果 SDK 向國泰證券送出委託。

        ── 真實實作步驟（取消下方註解）────────────────────────────────────
        from fugle_trade.sdk import SDK

        sdk = self._get_sdk()

        fugle_order = sdk.Order(
            buy_sell   = SDK.Action.Buy if order.Action == "BUY" else SDK.Action.Sell,
            stock_no   = order.Symbol,
            quantity   = order.Quantity // 1000,   # 富果以「張」為單位
            price      = order.Price,
            price_flag = SDK.PriceFlag.Limit if order.PriceType == "LMT" else SDK.PriceFlag.Market,
            bs_flag    = getattr(SDK.BSFlag, order.OrderType, SDK.BSFlag.ROD),
            trade      = SDK.Trade.Cash,
        )

        # ⚠ 真實下單前務必再次確認委託金額不超過 RISK_MAX_ORDER_AMOUNT
        resp = sdk.place_order(fugle_order)
        logger.info(f"[Fugle] 委託回傳：{resp}")

        return {
            "success":      True,
            "trade_id":     resp.get("ordNo", str(uuid.uuid4())),
            "action":       order.Action,
            "ticker":       order.Symbol,
            "shares":       order.Quantity,
            "price":        order.Price,
            "trade_amount": order.Price * order.Quantity,
            "commission":   resp.get("fee", 0),
            "stt":          resp.get("tax", 0),
            "total_fee":    resp.get("fee", 0) + resp.get("tax", 0),
            "net_amount":   resp.get("netAmount", 0),
            "realized_pnl": 0,    # 賣出時由券商回報計算
            "balance_before": 0,  # 需呼叫 get_balance() 前後各一次
            "balance_after":  0,
            "raw_response": resp,
        }
        ────────────────────────────────────────────────────────────────────
        """
        raise NotImplementedError(
            "CathayBroker.place_order() 尚未實作。\n"
            "請安裝 fugle-trade 並依照上方說明取消相關程式碼的註解。\n"
            "pip install fugle-trade"
        )

    def get_balance(self) -> dict:
        """
        ── 真實實作（取消下方註解）──────────────────────────────────────────
        sdk = self._get_sdk()
        inv = sdk.get_inventories()     # 庫存查詢
        bal = sdk.get_balance()         # 可用資金查詢
        return {
            "cash":         bal.get("availableMoney", 0),
            "total_assets": bal.get("totalAsset",     0),
            "realized_pnl": bal.get("realizedProfit", 0),
        }
        ────────────────────────────────────────────────────────────────────
        """
        raise NotImplementedError("CathayBroker.get_balance() 尚未實作。")

    def get_positions(self) -> list[dict]:
        """
        ── 真實實作（取消下方註解）──────────────────────────────────────────
        sdk = self._get_sdk()
        items = sdk.get_inventories()
        return [
            {
                "ticker":     i["stockNo"],
                "shares":     i["qty"] * 1000,
                "avg_cost":   i["costPrice"],
                "total_cost": i["costPrice"] * i["qty"] * 1000,
            }
            for i in items
        ]
        ────────────────────────────────────────────────────────────────────
        """
        raise NotImplementedError("CathayBroker.get_positions() 尚未實作。")


# ══════════════════════════════════════════════════════════════════════════════
#  實戰模式 - 元富證券 MasterLink API
# ══════════════════════════════════════════════════════════════════════════════

class MasterLinkBroker(BrokerInterface):
    """
    元富證券實戰介面。
    IS_SIMULATION=False 且 BROKER=masterlink 時啟用。

    ── 安裝 SDK ──────────────────────────────────────────────────────────────
    # 元富官方 Python SDK（須向元富申請開發者帳號取得）
    # pip install masterlink-mq-py（或聯絡元富取得最新版本）

    ── 必要環境變數 ──────────────────────────────────────────────────────────
    ML_API_KEY        : 元富 API Key
    ML_API_SECRET     : 元富 API Secret
    ML_CERT_PATH      : PKCS#12 憑證路徑（.pfx 或 .p12）
    ML_CERT_PASSWORD  : 憑證密碼
    ML_ACCOUNT_ID     : 元富帳號

    ── 雙因子驗證（2FA）注意事項 ───────────────────────────────────────────
    元富 API 可能要求 OTP（一次性密碼）：
    1. 程式啟動時提示輸入 OTP（適合手動觸發）
    2. 使用硬體 OTP Token（TOTP/HOTP）
    3. 聯絡元富業務確認 API 帳號的 2FA 需求

    ── 憑證與 API Key 安全規範（同 CathayBroker，請參閱上方說明）────────────
    重點提醒：
    • 憑證文件務必放在 .gitignore 的排除路徑
    • 生產環境強烈建議使用 Vault 服務管理所有 Secret
    • 網路層面加上 IP 白名單，只允許部署伺服器的 IP 存取 API
    """

    def __init__(self) -> None:
        self._api_key       = self._require_env("ML_API_KEY")
        self._api_secret    = self._require_env("ML_API_SECRET")
        self._cert_path     = self._require_env("ML_CERT_PATH")
        self._cert_password = self._require_env("ML_CERT_PASSWORD")
        self._account_id    = self._require_env("ML_ACCOUNT_ID")
        self._client        = None

    @staticmethod
    def _require_env(key: str) -> str:
        val = os.getenv(key, "").strip()
        if not val:
            raise EnvironmentError(f"實戰模式缺少環境變數：{key}，請參考 .env.example。")
        return val

    def _get_client(self):
        """
        取得已認證的元富 API 客戶端。

        ── 真實實作步驟（取消下方註解）────────────────────────────────────
        from masterlink_mq_py.MasterLink import MasterLink

        if self._client is None:
            self._client = MasterLink()
            # 憑證登入（所有憑證資料均來自環境變數）
            self._client.login(
                user_id  = self._account_id,
                password = os.getenv("ML_PASSWORD", ""),    # 僅用於初次登入取 Token
                cert_path = self._cert_path,
                cert_pass = self._cert_password,
            )
            logger.info("[MasterLink] 登入成功")

        return self._client
        ────────────────────────────────────────────────────────────────────
        """
        raise NotImplementedError(
            "MasterLinkBroker._get_client() 尚未實作。\n"
            "請向元富申請 SDK 並依照上方說明取消相關程式碼的註解。"
        )

    @property
    def broker_name(self) -> str:
        return "MasterLinkBroker（元富證券）"

    def place_order(self, order: Order) -> dict:
        """
        ── 真實實作（取消下方註解）──────────────────────────────────────────
        client = self._get_client()

        resp = client.order(
            buySell   = "B" if order.Action == "BUY" else "S",
            stockNo   = order.Symbol,
            qty       = order.Quantity,          # 元富以「股」為單位
            price     = str(order.Price),
            priceType = "L" if order.PriceType == "LMT" else "M",
            orderCond = {"ROD":"R","IOC":"I","FOK":"F"}.get(order.OrderType,"R"),
            tradeType = "0",                     # 0=現股
        )
        logger.info(f"[MasterLink] 委託回傳：{resp}")

        return {
            "success":      resp.get("result") == "0000",
            "trade_id":     resp.get("orderId", str(uuid.uuid4())),
            "action":       order.Action,
            "ticker":       order.Symbol,
            "shares":       order.Quantity,
            "price":        order.Price,
            "trade_amount": order.Price * order.Quantity,
            "commission":   resp.get("fee", 0),
            "stt":          resp.get("tax", 0),
            "total_fee":    resp.get("fee", 0) + resp.get("tax", 0),
            "net_amount":   resp.get("netAmt", 0),
            "realized_pnl": 0,
            "balance_before": 0,
            "balance_after":  0,
            "raw_response": resp,
        }
        ────────────────────────────────────────────────────────────────────
        """
        raise NotImplementedError("MasterLinkBroker.place_order() 尚未實作。")

    def get_balance(self) -> dict:
        """
        ── 真實實作（取消下方註解）──────────────────────────────────────────
        client = self._get_client()
        bal = client.query_balance()
        return {
            "cash":         float(bal.get("availableFunds", 0)),
            "total_assets": float(bal.get("totalAssets",    0)),
            "realized_pnl": float(bal.get("realizedPnL",   0)),
        }
        ────────────────────────────────────────────────────────────────────
        """
        raise NotImplementedError("MasterLinkBroker.get_balance() 尚未實作。")

    def get_positions(self) -> list[dict]:
        raise NotImplementedError("MasterLinkBroker.get_positions() 尚未實作。")


# ══════════════════════════════════════════════════════════════════════════════
#  Factory：依環境變數建立正確的 Broker 實例
# ══════════════════════════════════════════════════════════════════════════════

def create_broker(account: Optional[VirtualAccount] = None) -> BrokerInterface:
    """
    根據 IS_SIMULATION 環境變數建立對應的 Broker 實例。

    Parameters
    ----------
    account : VirtualAccount 實例（模擬模式必須傳入）

    Returns
    -------
    BrokerInterface 子類別實例
    """
    if IS_SIMULATION:
        if account is None:
            raise ValueError("模擬模式需要傳入 VirtualAccount 實例。")
        broker = SimulatedBroker(account)
        logger.info(f"[Broker] 使用 {broker.broker_name}")
        return broker

    # ── 實戰模式：依 BROKER 環境變數選擇券商 ───────────────────────────────
    logger.warning(
        "⚠️  IS_SIMULATION=False：即將連接真實券商 API！"
        "請確認所有 API Key 與憑證環境變數均已正確設定。"
    )
    if BROKER_NAME == "cathay":
        broker = CathayBroker()
    elif BROKER_NAME == "masterlink":
        broker = MasterLinkBroker()
    else:
        raise ValueError(
            f"未知的 BROKER 設定：'{BROKER_NAME}'。"
            "可選值：cathay | masterlink"
        )
    logger.info(f"[Broker] 使用 {broker.broker_name}")
    return broker


# ══════════════════════════════════════════════════════════════════════════════
#  Webhook 工具（與 v1 相同）
# ══════════════════════════════════════════════════════════════════════════════

def load_config() -> dict:
    defaults = {"url": "", "secret": "", "enabled": False, "async_send": True}
    if not os.path.exists(CONFIG_FILE):
        return defaults
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return {**defaults, **json.load(f)}
    except Exception:
        return defaults


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _compute_hmac(payload_str: str, secret: str) -> str:
    mac = hmac.new(secret.encode(), payload_str.encode(), hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def _fmt_money(n: float) -> str:
    return f"NT${n:,.0f}"


def _build_line_text(order: Order, trade: dict, account_snap: dict) -> str:
    action_icon = "▲ 買進" if order.Action == Action.BUY else "▼ 賣出"
    sep = "─" * 20
    lots_str = f"（{trade['shares'] // 1000} 張）" if trade['shares'] >= 1000 else "（零股）"
    pnl_line = ""
    if order.Action == Action.SELL:
        pnl = trade.get("realized_pnl", 0) or 0
        sign = "+" if pnl >= 0 else ""
        pnl_line = f"  已實現損益：{sign}{_fmt_money(pnl)}\n"

    mode_tag = "🟡模擬" if IS_SIMULATION else "🔴實戰"
    return (
        f"【{mode_tag} 交易成功】\n"
        f"{action_icon} {order.Symbol}\n"
        f"{sep}\n"
        f"📊 交易明細\n"
        f"  股數：{trade['shares']:,} 股 {lots_str}\n"
        f"  成交價：NT${trade['price']:,.2f}\n"
        f"  成交金額：{_fmt_money(trade['trade_amount'])}\n"
        f"  手續費：{_fmt_money(trade['commission'])}\n"
        f"  證交稅：{_fmt_money(trade['stt'])}\n"
        f"  {'實際付出' if order.Action == Action.BUY else '實際收入'}：{_fmt_money(trade['net_amount'])}\n"
        f"{pnl_line}"
        f"{sep}\n"
        f"💰 帳戶狀態\n"
        f"  現金餘額：{_fmt_money(account_snap['cash'])}\n"
        f"  總資產：{_fmt_money(account_snap['total_assets'])}\n"
        f"{sep}\n"
        f"📈 {order.Strategy}\n"
        f"  {order.SignalReason}\n"
        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )


def _build_email_html(order: Order, trade: dict, account_snap: dict) -> str:
    color = "#1a7f37" if order.Action == Action.BUY else "#8b1a1a"
    act   = "買進" if order.Action == Action.BUY else "賣出"
    mode  = "🟡 模擬交易" if IS_SIMULATION else "🔴 實戰交易"
    pnl   = trade.get("realized_pnl", 0) or 0

    def row(label, value):
        return f"<tr><td style='padding:4px 12px;color:#555'>{label}</td><td style='padding:4px 12px'>{value}</td></tr>"

    return f"""
<div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;border:1px solid #e0e0e0;border-radius:8px;overflow:hidden">
  <div style="background:{color};color:#fff;padding:14px 18px">
    <h2 style="margin:0;font-size:18px">[{mode}] {act} {order.Symbol}</h2>
    <div style="font-size:12px;opacity:.8;margin-top:4px">券商：{order.Strategy.split('_')[0] if '_' in order.Strategy else '手動'} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
  </div>
  <div style="padding:16px 18px">
    <table style="width:100%;font-size:13px;border-collapse:collapse">
      {row('代號', f"<b>{order.Symbol}</b>")}
      {row('買賣', f"<span style='color:{color};font-weight:bold'>{act}</span>")}
      {row('股數', f"{trade['shares']:,} 股")}
      {row('成交價', f"NT${trade['price']:,.2f}")}
      {row('成交金額', _fmt_money(trade['trade_amount']))}
      {row('手續費', _fmt_money(trade['commission']))}
      {row('證交稅', _fmt_money(trade['stt']))}
      {row('實際付出/收入', f"<b>{_fmt_money(trade['net_amount'])}</b>")}
      {row('已實現損益', f"<span style='color:{\"#1a7f37\" if pnl>=0 else \"#8b1a1a\"}'>{('+' if pnl>=0 else '')}{_fmt_money(pnl)}</span>") if order.Action == Action.SELL else ''}
    </table>
    <h3 style="font-size:14px;color:#333;border-bottom:1px solid #eee;padding-bottom:6px;margin-top:16px">帳戶狀態</h3>
    <table style="width:100%;font-size:13px;border-collapse:collapse">
      {row('現金餘額', _fmt_money(account_snap['cash']))}
      {row('總資產', _fmt_money(account_snap['total_assets']))}
    </table>
  </div>
  <div style="background:#f5f5f5;padding:8px 18px;font-size:11px;color:#999;text-align:center">
    {'此為模擬交易通知，非真實交易。' if IS_SIMULATION else '此為真實交易通知。'}
  </div>
</div>"""


def build_payload(order: Order, trade_result: dict, account_snap: dict) -> dict:
    action_zh   = "買進" if order.Action == Action.BUY else "賣出"
    action_icon = "▲" if order.Action == Action.BUY else "▼"
    return {
        "event":        "TRADE_EXECUTED",
        "event_id":     str(uuid.uuid4()),
        "timestamp":    datetime.now().isoformat(),
        "system":       "台股模擬交易系統",
        "is_simulation": IS_SIMULATION,
        "broker":       BROKER_NAME if not IS_SIMULATION else "simulation",
        "trade": {
            "trade_id":      trade_result.get("trade_id", ""),
            "trade_time":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "action":        order.Action,
            "ticker":        order.Symbol,
            "shares":        trade_result.get("shares", order.Quantity),
            "price":         trade_result.get("price", order.Price),
            "trade_amount":  trade_result.get("trade_amount", 0),
            "commission":    trade_result.get("commission", 0),
            "stt":           trade_result.get("stt", 0),
            "total_fee":     trade_result.get("total_fee", 0),
            "net_amount":    trade_result.get(
                                "net_paid" if order.Action == Action.BUY else "net_received",
                                trade_result.get("net_amount", 0)),
            "realized_pnl":  trade_result.get("realized_pnl", 0),
            "balance_before": trade_result.get("balance_before", 0),
            "balance_after":  trade_result.get("balance_after", 0),
        },
        "order":   asdict(order),
        "account": {
            "cash":           account_snap.get("cash", 0),
            "holdings_value": account_snap.get("holdings_value", 0),
            "total_assets":   account_snap.get("total_assets", 0),
            "realized_pnl":   account_snap.get("realized_pnl", 0),
            "unrealized_pnl": account_snap.get("unrealized_pnl", 0),
        },
        "notification": {
            "title":         f"【{'模擬' if IS_SIMULATION else '實戰'}交易】{action_icon} {action_zh} {order.Symbol}",
            "summary":       f"{order.Symbol} {action_zh} {order.Quantity:,}股 @{order.Price:.2f}，餘額 NT${account_snap.get('cash', 0):,.0f}",
            "line_text":     _build_line_text(order, trade_result, account_snap),
            "email_subject": f"[台股{'模擬' if IS_SIMULATION else '實戰'}] {action_icon}{action_zh} {order.Symbol} {order.Quantity:,}股 @{order.Price:.2f}",
            "email_html":    _build_email_html(order, trade_result, account_snap),
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
#  主類別：OrderExecutor（公開介面與 v1 相同，無需修改 app.py）
# ══════════════════════════════════════════════════════════════════════════════

class OrderExecutor:
    """
    訂單執行器（雙模式版）。

    IS_SIMULATION=True  → 使用 SimulatedBroker（虛擬帳戶，預設）
    IS_SIMULATION=False → 使用 CathayBroker / MasterLinkBroker（實戰）

    ⚠ 切換至實戰模式前的檢查清單：
    □ IS_SIMULATION=false 已設定
    □ BROKER 對應的所有環境變數已設定且通過驗證
    □ 憑證文件已放置於安全路徑（不在版本控制目錄內）
    □ RISK_MAX_ORDER_AMOUNT 已設定合理的單筆上限
    □ 已在「測試帳戶」上驗證過所有下單流程
    □ 告警監控已就緒（Webhook / LINE 通知正常）
    """

    def __init__(
        self,
        account:         Optional[VirtualAccount] = None,
        webhook_url:     str  = "",
        webhook_secret:  str  = "",
        async_send:      bool = True,
    ) -> None:
        self._broker        = create_broker(account)
        self.webhook_url    = webhook_url
        self.webhook_secret = webhook_secret
        self.async_send     = async_send
        self._logs: deque[dict] = deque(maxlen=LOG_KEEP)
        self._daily_order_count = 0
        self._last_count_date   = datetime.now().date()

    # ── 風險控管 ───────────────────────────────────────────────────────────

    def _check_risk(self, order: Order) -> Optional[str]:
        """
        執行前風控檢查（模擬與實戰均適用）。

        Returns
        -------
        None  : 通過風控
        str   : 風控拒絕原因
        """
        trade_amount = order.Price * order.Quantity

        # 重置每日計數器
        today = datetime.now().date()
        if today != self._last_count_date:
            self._daily_order_count = 0
            self._last_count_date   = today

        if trade_amount > RISK_MAX_ORDER_AMOUNT:
            return (
                f"委託金額 NT${trade_amount:,.0f} 超過每筆上限 "
                f"NT${RISK_MAX_ORDER_AMOUNT:,.0f}（MAX_ORDER_AMOUNT）"
            )
        if order.Quantity > RISK_MAX_SHARES:
            return (
                f"委託股數 {order.Quantity:,} 超過每筆上限 "
                f"{RISK_MAX_SHARES:,}（MAX_SHARES）"
            )
        if self._daily_order_count >= RISK_MAX_DAILY_ORDERS:
            return (
                f"今日已下單 {self._daily_order_count} 次，"
                f"達每日上限 {RISK_MAX_DAILY_ORDERS}（MAX_DAILY_ORDERS）"
            )
        return None

    # ── 主執行方法 ─────────────────────────────────────────────────────────

    def execute(self, order: Order) -> dict:
        """
        執行訂單完整流程（公開介面，與 v1 相同）。

        1. 基本驗證
        2. 風控檢查
        3. 透過 Broker 介面執行（模擬 or 實戰）
        4. 取得帳戶快照
        5. 非同步發送 Webhook
        """
        # 驗證
        err = self._validate(order)
        if err:
            return {"success": False, "error": err}

        # 風控
        risk_err = self._check_risk(order)
        if risk_err:
            logger.warning(f"[風控] 拒絕委託：{risk_err}")
            return {"success": False, "error": f"[風控] {risk_err}"}

        # 執行
        try:
            trade_result = self._broker.place_order(order)
        except (ValueError, NotImplementedError) as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error(f"[Broker] place_order 錯誤：{e}")
            return {"success": False, "error": f"券商 API 錯誤：{e}"}

        self._daily_order_count += 1

        # 帳戶快照
        try:
            account_snap = self._broker.get_balance()
            account_snap.setdefault("holdings_value", 0)
            account_snap.setdefault("unrealized_pnl", 0)
        except Exception:
            account_snap = {"cash": 0, "total_assets": 0, "realized_pnl": 0,
                            "holdings_value": 0, "unrealized_pnl": 0}

        # Webhook
        payload        = build_payload(order, trade_result, account_snap)
        webhook_status = self._dispatch_webhook(payload)

        return {
            "success":      True,
            "trade":        trade_result,
            "payload":      payload,
            "webhook_sent": webhook_status,
            "broker":       self._broker.broker_name,
            "is_simulation": IS_SIMULATION,
        }

    def set_webhook(self, url: str, secret: str = "", enabled: bool = True) -> None:
        self.webhook_url    = url if enabled else ""
        self.webhook_secret = secret
        save_config({"url": url, "secret": secret, "enabled": enabled, "async_send": self.async_send})

    def send_test_webhook(self) -> dict:
        if not self.webhook_url:
            return {"success": False, "error": "Webhook URL 未設定"}
        payload = {
            "event":        "TEST",
            "event_id":     str(uuid.uuid4()),
            "timestamp":    datetime.now().isoformat(),
            "system":       "台股模擬交易系統",
            "is_simulation": IS_SIMULATION,
            "message":      "Webhook 連線測試成功 ✅",
            "notification": {
                "title":         "【Webhook 測試】連線正常",
                "summary":       "n8n 已成功接收測試訊號。",
                "line_text":     f"【Webhook 測試成功 ✅】\n模式：{'模擬' if IS_SIMULATION else '實戰'}\n時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                "email_subject": "[台股模擬] Webhook 連線測試成功",
                "email_html":    "<h2>Webhook 測試成功 ✅</h2>",
            },
        }
        return self._send_webhook_with_retry(payload)

    @property
    def webhook_logs(self) -> list[dict]:
        return list(reversed(self._logs))

    @property
    def mode(self) -> str:
        return "simulation" if IS_SIMULATION else "live"

    @property
    def broker_name(self) -> str:
        return self._broker.broker_name

    # ── 內部方法（與 v1 相同）────────────────────────────────────────────

    def _validate(self, order: Order) -> Optional[str]:
        if order.Action not in (Action.BUY, Action.SELL):
            return f"無效 Action：{order.Action}"
        if not order.Symbol:
            return "Symbol 不得為空"
        if order.Price <= 0:
            return f"Price 必須 > 0（目前：{order.Price}）"
        if order.Quantity <= 0:
            return f"Quantity 必須 > 0（目前：{order.Quantity}）"
        return None

    def _dispatch_webhook(self, payload: dict) -> str:
        if not self.webhook_url:
            return "disabled"
        if self.async_send:
            threading.Thread(
                target=self._send_webhook_with_retry, args=(payload,), daemon=True
            ).start()
            return "async"
        result = self._send_webhook_with_retry(payload)
        return "sync_ok" if result["success"] else "sync_fail"

    def _send_webhook_with_retry(self, payload: dict) -> dict:
        payload_str = json.dumps(payload, ensure_ascii=False, default=str)
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "X-Source":     "tw-stock-simulator",
            "X-Event":      payload.get("event", "UNKNOWN"),
            "X-Mode":       self.mode,
        }
        if self.webhook_secret:
            headers["X-Signature-256"] = _compute_hmac(payload_str, self.webhook_secret)

        last_error = ""
        for attempt in range(MAX_RETRY):
            if attempt > 0:
                time.sleep(RETRY_DELAYS[attempt - 1])
            try:
                resp    = requests.post(self.webhook_url, data=payload_str.encode(),
                                        headers=headers, timeout=WEBHOOK_TIMEOUT)
                success = 200 <= resp.status_code < 300
                entry   = {
                    "time":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "event":     payload.get("event", ""),
                    "ticker":    payload.get("trade", {}).get("ticker", ""),
                    "action":    payload.get("trade", {}).get("action", ""),
                    "status":    resp.status_code,
                    "success":   success,
                    "attempt":   attempt + 1,
                    "url":       self.webhook_url,
                    "resp_text": resp.text[:200],
                }
                self._logs.append(entry)
                if success:
                    return {"success": True, "status_code": resp.status_code, "attempt": attempt + 1}
                last_error = f"HTTP {resp.status_code}"
            except requests.exceptions.Timeout:
                last_error = f"逾時（>{WEBHOOK_TIMEOUT}s）"
            except requests.exceptions.ConnectionError as e:
                last_error = f"連線失敗：{e}"
            except Exception as e:
                last_error = str(e)

        self._logs.append({"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                           "event": payload.get("event",""), "success": False,
                           "error": last_error, "attempt": MAX_RETRY, "url": self.webhook_url})
        return {"success": False, "error": last_error, "attempt": MAX_RETRY}
