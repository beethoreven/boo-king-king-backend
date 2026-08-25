# booking-king-backend

劇本殺預約系統後端。機房 Singapore (ap-southeast-1)，須與 Neon 資料庫同區。

## 本機開發

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填入實際 DATABASE_URL
```

## 已定案的架構決策

- 連線層：`db/connection.py`，psycopg3 + `ConnectionPool`，參數與理由見檔案內註解
- 防超賣：`UNIQUE (session_id, seat_no)` + 冪等鍵 `UNIQUE (request_id)`，同一交易
- 重試：只在應用層、以 `request_id` 為鍵；連線層/pool 層自動重試會製造超賣
- 上線前檢查：所有 serial 欄位驗證 `last_value >= MAX(id)`
