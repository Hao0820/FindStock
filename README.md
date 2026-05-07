# Taiwan Stock Screener

這個小工具會使用 Yahoo Finance 資料，篩選：

- 去年年初到年尾漲幅超過 50%，或今年年初到現在漲幅超過 30% 的台股
- 目前最新價格距離日線、週線或月線的 `12EMA`、布林中軌或布林下軌在容忍範圍內（預設：日線 1%、週線 3%、月線 5%）
- 布林軌道參數固定為 `21, 2.1`

## 安裝

```bash
cd /Users/user/Documents/projects/stock
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 執行

```bash
python main.py
```

常用參數：

```bash
python main.py --max-tickers 50
python main.py --tolerance-pct 1
python main.py --weekly-tolerance-pct 3
python main.py --monthly-tolerance-pct 5
python main.py --min-gain 30
python main.py --chunk-size 50
```

終端機只會顯示處理進度與最後摘要，完整結果會輸出成 `outputs/` 內的 CSV 檔。

## 篩選邏輯

程式目前採用以下假設：

- 股票池以 `twstock` 為基底，並用 TWSE / TPEx 官方清單補進上市、上櫃、興櫃 4 位數股票
- 使用 Yahoo Finance 的調整後日線收盤價
- 漲幅會分別計算去年整年與今年至今
- `EMA12` 會另外計算，並拿來判斷當前價格是否接近 `12EMA`
- 布林軌道參數固定為 `21, 2.1`
- 符合條件定義為：
  去年整年漲幅超過 50%，或今年至今漲幅超過 30%
- 同時最新價格距離 `12EMA`、`middle` 或 `lower` 在容忍值內（日線預設 1%、週線預設 3%、月線預設 5%）
- 檢查範圍包含日線、週線與月線
- 下載時會分批處理股票，降低 Yahoo 限流機率
- 輸出的 `註解` 欄位會標示是符合「去年漲幅超過50%」、「今年漲幅超過30%」，或兩者皆符合

如果你要把「中軌或下軌」改成更嚴格的「接近中軌」或「碰下軌」，我可以再幫你把條件改成百分比容忍區間。
