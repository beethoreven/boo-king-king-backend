# booking-king-backend

劇本殺預約系統後端。機房 Singapore (ap-southeast-1)，須與 Neon 資料庫同區。

## 本機開發

第一次設定：

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填入實際 DATABASE_URL 與 GOOGLE_CLIENT_ID
```

啟動（每次開發都是這一行）：

```bash
./venv/bin/python app.py
```

直接指向 venv 裡的直譯器，不需要先 `activate`。跑在 **5001**，不是 5000——
macOS 的 AirPlay 接收器佔用 5000，而且是回 403 不是拒絕連線，症狀看起來會
像應用程式自己壞了。

被佔用時的處理見 `release-occupied-port` skill；不要改用隨機 port，
Google OAuth 的 authorized origins 是連 port 一起比對的。

確認起來了：

```bash
curl -s localhost:5001/api/health
```

前端在另一個 repo（booking-king-web），用 `python3 -m http.server 5173` 起，
但**網址一定要帶 `?apiBase=http://localhost:5001`**，否則前端會把 API 打到
5173 自己身上，症狀是登入畫面卡住或 404。

## 已定案的架構決策

- 連線層：`db/connection.py`，psycopg3 + `ConnectionPool`，參數與理由見檔案內註解
- 防超賣：per-slot advisory lock（鍵是「劇本+日期+時間」）鎖住該場次後才數容量，
  容量 = `1 + mmg.waitlist_limit` 組；主持人撞期另有 per-host advisory lock，
  多把鎖依排序取得避免死鎖。實作見 `db/bookings.py` 的 `_lock_slot` / `_lock_hosts`
- 冪等鍵：`UNIQUE (request_id)` + `ON CONFLICT DO NOTHING`，由前端在「按下送出」
  那一刻產生一組 UUID；日期/時間/主持人任一改變就換新的，否則改了日期會被當成同一筆
- 重試：只在應用層、以 `request_id` 為鍵；連線層/pool 層自動重試會製造超賣
- 上線前檢查：所有 serial 欄位驗證 `last_value >= MAX(id)`
