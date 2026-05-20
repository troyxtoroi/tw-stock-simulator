"""
Flask 後端 API（含 OrderExecutor 整合）
========================================
模組串接：tw_stock_quote → strategy → order_executor → virtual_account → webhook

啟動：
  pip install flask yfinance pandas requests
  python app.py  →  http://localhost:5000
"""

import json
import math
import os
import sys

from flask import Flask, jsonify, render_template, request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tw_stock_quote import get_historical_kline, get_realtime_quote
from virtual_account import VirtualAccount, calculate_fees, INITIAL_CAPITAL, PROFIT_TARGET
from strategy import MACrossStrategy, Order, Action, OrderType, PriceType
from order_executor import OrderExecutor, load_config, save_config, IS_SIMULATION, BROKER_NAME


# ── 初始化 ─────────────────────────────────────────────────────────────────────

app     = Flask(__name__)
account = VirtualAccount(commission_discount=0.6)

# 從設定檔載入 Webhook 設定並建立執行器
_cfg     = load_config()
executor = OrderExecutor(
    account        = account,
    webhook_url    = _cfg.get("url", ""),
    webhook_secret = _cfg.get("secret", ""),
    async_send     = _cfg.get("async_send", True),
)

DEFAULT_SHORT = 5
DEFAULT_LONG  = 20


# ── JSON 工具 ──────────────────────────────────────────────────────────────────

def _clean(obj):
    """遞迴將 NaN / Inf 替換為 None（解決 JSON 序列化問題）。"""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(i) for i in obj]
    return obj


def sjson(data, status: int = 200):
    text = json.dumps(_clean(data), ensure_ascii=False, default=str)
    return app.response_class(text, status=status, mimetype="application/json")


# ══════════════════════════════════════════════════════════════════════════════
#  頁面
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


# ══════════════════════════════════════════════════════════════════════════════
#  報價 API
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/history")
def api_history():
    """GET /api/history?ticker=0050&period=6mo&short=5&long=20"""
    ticker  = request.args.get("ticker", "0050").upper()
    period  = request.args.get("period",  "6mo")
    interval= request.args.get("interval","1d")
    short_w = int(request.args.get("short", DEFAULT_SHORT))
    long_w  = int(request.args.get("long",  DEFAULT_LONG))

    df = get_historical_kline(ticker, period=period, interval=interval)
    if df.empty:
        return sjson({"error": f"查無 {ticker} 的歷史資料"}, 404)

    strategy = MACrossStrategy(short_window=short_w, long_window=long_w)
    result   = strategy.run(df, ticker=ticker)
    return sjson(result)


@app.route("/api/realtime")
def api_realtime():
    """GET /api/realtime?tickers=0050,2330"""
    tickers = [t.strip().upper() for t in request.args.get("tickers", "0050").split(",") if t.strip()]
    df = get_realtime_quote(tickers, source="auto")
    return sjson(df.to_dict(orient="records") if not df.empty else [])


# ══════════════════════════════════════════════════════════════════════════════
#  策略 API
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/strategy/run")
def api_strategy_run():
    """GET /api/strategy/run?ticker=0050&period=1y&short=5&long=20&qty=1000"""
    ticker  = request.args.get("ticker", "0050").upper()
    period  = request.args.get("period", "1y")
    short_w = int(request.args.get("short", DEFAULT_SHORT))
    long_w  = int(request.args.get("long",  DEFAULT_LONG))
    qty     = int(request.args.get("qty",   1000))

    df = get_historical_kline(ticker, period=period)
    if df.empty:
        return sjson({"error": f"查無 {ticker}"}, 404)

    strategy = MACrossStrategy(short_window=short_w, long_window=long_w, default_qty=qty)
    return sjson(strategy.run(df, ticker=ticker, quantity=qty))


# ══════════════════════════════════════════════════════════════════════════════
#  下單 API（透過 OrderExecutor 串接所有模組）
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/order", methods=["POST"])
def api_order():
    """
    POST /api/order
    Body (JSON):
      {
        "Action":    "BUY" | "SELL",
        "Symbol":    "0050",
        "Price":     185.5,
        "Quantity":  1000,
        "OrderType": "ROD",      // 可選，預設 ROD
        "PriceType": "LMT",      // 可選，預設 LMT
        "Strategy":  "手動下單",  // 可選
        "SignalReason": "",       // 可選
        "SignalTime": ""          // 可選
      }

    回傳：
      {
        "success": true,
        "trade":   { ...交易明細... },
        "webhook_sent": "async" | "sync_ok" | "disabled",
        "payload": { ...完整 Webhook Payload... }
      }
    """
    body = request.get_json(force=True) or {}

    action   = body.get("Action", "").upper()
    ticker   = body.get("Symbol", "").upper()
    price    = float(body.get("Price", 0))
    quantity = int(body.get("Quantity", 0))

    if action not in ("BUY", "SELL"):
        return sjson({"error": "Action 必須為 BUY 或 SELL"}, 400)
    if not ticker:
        return sjson({"error": "Symbol 不得為空"}, 400)
    if price <= 0:
        return sjson({"error": "Price 必須大於 0"}, 400)
    if quantity <= 0:
        return sjson({"error": "Quantity 必須大於 0"}, 400)

    # 組裝 Order 物件（讓 OrderExecutor 統一處理）
    order = Order(
        Action       = action,
        Symbol       = ticker,
        Price        = price,
        Quantity     = quantity,
        OrderType    = body.get("OrderType", OrderType.ROD),
        PriceType    = body.get("PriceType", PriceType.LMT),
        Strategy     = body.get("Strategy",  "手動下單"),
        SignalReason = body.get("SignalReason", ""),
        SignalTime   = body.get("SignalTime", ""),
    )

    result = executor.execute(order)
    if not result["success"]:
        return sjson({"error": result["error"]}, 400)

    return sjson(result)


@app.route("/api/order/preview", methods=["POST"])
def api_order_preview():
    """POST /api/order/preview — 費用預覽，不實際執行。"""
    body     = request.get_json(force=True) or {}
    action   = body.get("Action", "BUY").upper()
    ticker   = body.get("Symbol", "0050").upper()
    price    = float(body.get("Price", 0))
    quantity = int(body.get("Quantity", 0))

    if price <= 0 or quantity <= 0:
        return sjson({"error": "Price 與 Quantity 必須大於 0"}, 400)

    fee = calculate_fees(ticker, quantity, price, action)
    return sjson({
        "trade_amount": fee.trade_amount,
        "commission":   fee.commission,
        "stt":          fee.stt,
        "total_fee":    fee.total_fee,
        "net_amount":   fee.net_amount,
        "action":       action,
    })


# ══════════════════════════════════════════════════════════════════════════════
#  帳戶 API
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/account/summary")
def api_account_summary():
    """GET /api/account/summary?prices=0050:185.5,2330:920"""
    prices_str = request.args.get("prices", "")
    current_prices: dict = {}
    if prices_str:
        for item in prices_str.split(","):
            parts = item.strip().split(":")
            if len(parts) == 2:
                try:
                    current_prices[parts[0].upper()] = float(parts[1])
                except ValueError:
                    pass
    report = account.summary(current_prices=current_prices or None)
    return sjson(report)


@app.route("/api/account/holdings")
def api_account_holdings():
    df = account.get_holdings()
    return sjson(df.to_dict(orient="records"))


@app.route("/api/account/history")
def api_account_history():
    """GET /api/account/history?ticker=0050&type=BUY&limit=50"""
    ticker     = request.args.get("ticker", None)
    trade_type = request.args.get("type",   None)
    limit      = int(request.args.get("limit", 50))
    df = account.get_trade_history(ticker=ticker, trade_type=trade_type, limit=limit)
    return sjson(df.to_dict(orient="records"))


@app.route("/api/system/status")
def api_system_status():
    """GET /api/system/status — 回傳系統模式與目前設定摘要。"""
    cfg = load_config()
    return sjson({
        "is_simulation": IS_SIMULATION,
        "mode":          "simulation" if IS_SIMULATION else "live",
        "mode_label":    "🟡 模擬模式" if IS_SIMULATION else "🔴 實戰模式",
        "broker":        "simulation" if IS_SIMULATION else BROKER_NAME,
        "broker_name":   executor.broker_name,
        "webhook": {
            "enabled":    cfg.get("enabled", False),
            "has_url":    bool(cfg.get("url", "")),
            "async_send": cfg.get("async_send", True),
        },
        "risk": {
            "max_order_amount": float(os.getenv("MAX_ORDER_AMOUNT", "500000")),
            "max_shares":       int(os.getenv("MAX_SHARES",         "10000")),
            "max_daily_orders": int(os.getenv("MAX_DAILY_ORDERS",   "20")),
        },
    })


@app.route("/api/account/reset", methods=["POST"])
def api_account_reset():
    """POST /api/account/reset — 重置帳戶（清空所有資料）。"""
    try:
        with account._conn() as conn:
            conn.execute("DELETE FROM trade_records")
            conn.execute("DELETE FROM holdings")
            conn.execute("DELETE FROM account_balance")
            conn.execute("""
                INSERT INTO account_balance
                    (cash, total_invested, realized_pnl,
                     initial_capital, profit_target, updated_at)
                VALUES (?, 0, 0, ?, ?, datetime('now','localtime'))
            """, (INITIAL_CAPITAL, INITIAL_CAPITAL, PROFIT_TARGET))
            conn.commit()
        return sjson({"success": True, "message": "帳戶已重置"})
    except Exception as e:
        return sjson({"error": str(e)}, 500)


# ══════════════════════════════════════════════════════════════════════════════
#  Webhook API
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/webhook/config", methods=["GET", "POST"])
def api_webhook_config():
    """
    GET  → 回傳目前 Webhook 設定（不含 secret）
    POST → 更新設定
      Body: { "url": "...", "secret": "...", "enabled": true, "async_send": true }
    """
    if request.method == "GET":
        cfg = load_config()
        # 不回傳 secret 明文（僅告知是否有設定）
        return sjson({
            "url":        cfg.get("url", ""),
            "enabled":    cfg.get("enabled", False),
            "async_send": cfg.get("async_send", True),
            "has_secret": bool(cfg.get("secret", "")),
        })

    body    = request.get_json(force=True) or {}
    url     = body.get("url", "").strip()
    secret  = body.get("secret", "").strip()
    enabled = bool(body.get("enabled", True))
    async_s = bool(body.get("async_send", True))

    executor.set_webhook(url, secret=secret, enabled=enabled)
    executor.async_send = async_s
    save_config({"url": url, "secret": secret, "enabled": enabled, "async_send": async_s})

    return sjson({"success": True, "message": "Webhook 設定已儲存"})


@app.route("/api/webhook/test", methods=["POST"])
def api_webhook_test():
    """POST /api/webhook/test — 發送測試 Webhook 到 n8n。"""
    result = executor.send_test_webhook()
    return sjson(result, 200 if result["success"] else 502)


@app.route("/api/webhook/logs")
def api_webhook_logs():
    """GET /api/webhook/logs — 最近 N 筆 Webhook 發送紀錄。"""
    limit = int(request.args.get("limit", 20))
    return sjson(executor.webhook_logs[:limit])


# ══════════════════════════════════════════════════════════════════════════════
#  啟動
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    cfg = load_config()
    print("=" * 58)
    print("  台股模擬交易系統 v3.0")
    print("  http://localhost:5000")
    print(f"  Webhook：{'✅ 啟用 → ' + cfg.get('url','') if cfg.get('enabled') else '⛔ 停用'}")
    print("=" * 58)
    app.run(host="0.0.0.0", port=5000, debug=True)
