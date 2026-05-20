# 台股模擬交易系統 🏦

> 一個完整的台灣股市模擬交易系統，支援歷史 K 線、均線交叉策略、虛擬帳戶下單、Webhook 自動通知，並預留實戰券商 API 介面。

---

## 功能總覽

| 模組 | 功能 |
|------|------|
| `tw_stock_quote.py` | yfinance 歷史 K 線 + TWSE OpenAPI 即時報價 |
| `virtual_account.py` | 虛擬帳戶（SQLite）+ 台股手續費/證交稅計算 |
| `strategy.py` | 均線交叉策略（MA5×MA20）+ 標準化 Order 物件 |
| `order_executor.py` | 串接所有模組 + Webhook 通知（n8n/LINE/Email）|
| `app.py` | Flask REST API 後端 |
| `templates/index.html` | 深色主題交易介面（K線圖 + 下單 + 帳戶報表）|

---

## 快速啟動

### 1. 安裝依賴
```bash
pip install -r requirements.txt
```

### 2. 設定環境變數（可選）
```bash
cp .env.example .env
# 編輯 .env，設定 Webhook URL 等參數
```

### 3. 啟動伺服器
```bash
python app.py
```

### 4. 開啟瀏覽器
```
http://localhost:5000
```

---

## 系統架構

```
使用者瀏覽器
    │
    ▼
Flask API (app.py)
    │
    ├─── tw_stock_quote.py ──► yfinance / TWSE API
    │         ↓
    ├─── strategy.py ──────► 均線計算 + Order 物件
    │         ↓
    ├─── order_executor.py ─► IS_SIMULATION 開關
    │         ├── True  → virtual_account.py → SQLite
    │         └── False → CathayBroker / MasterLinkBroker（實戰）
    │                            ↓
    └─────────────────────────► Webhook → n8n → LINE/Email
```

---

## 網頁介面

### 主畫面功能
- **K 線圖**：ApexCharts 蠟燭圖，疊加 MA5（黃）/ MA20（藍）均線
- **策略訊號**：自動標記黃金交叉（▲買）/ 死亡交叉（▼賣），可一鍵執行
- **下單面板**：買進/賣出、股數、價格、委託類型選擇，即時費用預覽
- **帳戶報表**：現金、持倉市值、已/未實現損益、目標進度條
- **交易紀錄**：完整費用拆解（手續費 + 證交稅 + 損益）
- **Webhook 設定**：n8n URL、HMAC 簽章、發送測試、發送紀錄

---

## API 端點

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/history` | 歷史 K 線 + 均線（`?ticker=0050&period=6mo`）|
| GET | `/api/realtime` | 即時報價（`?tickers=0050,2330`）|
| GET | `/api/strategy/run` | 執行均線策略（`?ticker=0050&short=5&long=20`）|
| POST | `/api/order` | 執行委託（含 Webhook 通知）|
| POST | `/api/order/preview` | 費用預覽（不實際下單）|
| GET | `/api/account/summary` | 帳戶損益報表 |
| GET | `/api/account/holdings` | 持倉列表 |
| GET | `/api/account/history` | 交易紀錄 |
| POST | `/api/account/reset` | 重置帳戶 |
| GET | `/api/system/status` | 系統模式與設定 |
| GET/POST | `/api/webhook/config` | Webhook 設定 |
| POST | `/api/webhook/test` | 發送測試 Webhook |
| GET | `/api/webhook/logs` | 發送紀錄 |

---

## 台股費用計算

```
手續費 = max(成交金額 × 0.1425% × 折數,  NT$20)   ← 買賣皆收
證交稅 = 成交金額 × 0.3%（股票）/ 0.1%（ETF）     ← 僅賣出收取
```

預設手續費折數：**0.6（六折）**，可在 `VirtualAccount(commission_discount=0.6)` 調整。

---

## 均線交叉策略

```
黃金交叉：MA5 從下方突破 MA20 → BUY
死亡交叉：MA5 從上方跌破 MA20 → SELL
```

**Order 物件欄位（對應國泰/元富券商 API）：**

| 欄位 | 說明 | 國泰 | 元富 |
|------|------|------|------|
| `Action` | BUY / SELL | `BSFlag` | `buySell` |
| `Symbol` | 股票代號 | `StockNo` | `stockNo` |
| `Price` | 委託價格 | `Price` | `price` |
| `Quantity` | 委託股數 | `Qty`（張）| `qty`（股）|
| `OrderType` | ROD/IOC/FOK | `OrderCond` | `orderCond` |
| `PriceType` | LMT/MKT | `PriceType` | `priceType` |

---

## IS_SIMULATION 雙模式

```bash
# 模擬模式（預設，安全）
IS_SIMULATION=true python app.py

# 實戰模式（需設定券商環境變數）
IS_SIMULATION=false BROKER=cathay python app.py
```

實戰模式需設定的環境變數，請參考 [.env.example](.env.example)。

---

## n8n Webhook 串接

每筆下單成功後，系統自動 POST 以下 payload 到指定 URL：

```json
{
  "event": "TRADE_EXECUTED",
  "trade":  { "action", "ticker", "shares", "price", "net_amount", "realized_pnl" },
  "account": { "cash", "total_assets" },
  "notification": {
    "line_text":     "可直接填入 LINE node",
    "email_subject": "Email 主旨",
    "email_html":    "HTML 完整排版"
  }
}
```

**n8n 流程範例：**
1. Webhook Trigger → 接收 POST
2. Switch（`trade.action == "BUY"`）→ 分流
3. LINE node：`{{ $json.notification.line_text }}`
4. Gmail node：`{{ $json.notification.email_html }}`

---

## 初始設定

| 項目 | 數值 |
|------|------|
| 虛擬本金 | NT$100,000 |
| 收益目標 | NT$5,000,000 |
| 預設手續費折數 | 六折（0.6）|
| 資料庫 | `tw_stock_sim.db`（SQLite）|

---

## 專案結構

```
tw-stock-simulator/
├── app.py                  # Flask 後端
├── tw_stock_quote.py       # 報價模組
├── virtual_account.py      # 虛擬帳戶模組
├── strategy.py             # 策略運算模組
├── order_executor.py       # 訂單執行模組（雙模式）
├── templates/
│   └── index.html          # 前端交易介面
├── requirements.txt        # Python 依賴
├── .env.example            # 環境變數範本
├── .gitignore              # Git 排除清單
├── tw_stock_sim.db         # SQLite 資料庫（自動生成，不納入版控）
└── webhook_config.json     # Webhook 設定（自動生成，不納入版控）
```

---

## 安全注意事項

- `.env`、`*.pfx`、`tw_stock_sim.db` 已加入 `.gitignore`
- 實戰模式的 API Key 與憑證請使用 Secrets Manager 管理
- 切換至實戰前，請完整閱讀 `order_executor.py` 中的安全說明

---

## License

MIT License — 僅供學習與模擬練習，不構成投資建議。
