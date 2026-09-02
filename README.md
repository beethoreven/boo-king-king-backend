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
直接開 <http://localhost:5173> 就好——前端偵測到跑在 localhost 上會自動打
`http://localhost:5001`，不用帶參數。要對別的後端測就帶 `?apiBase=`，
查詢參數永遠優先（見 `js/api.js` 的 `API_BASE`）。

## 已定案的架構決策

- 連線層：`db/connection.py`，**每次請求各自開一條連線 + `BoundedSemaphore(5)` 當
  反壓上限**。原本用 psycopg3 的 `ConnectionPool`，在 Render 免費方案上會永久卡死
  （容器凍結後池子的背景執行緒不恢復），2026-08-31 實測後換掉，完整理由見檔案內註解
- 防超賣：per-slot advisory lock（鍵是「劇本+日期+時間」）鎖住該場次後才數容量，
  容量 = `1 + mmg.waitlist_limit` 組；主持人撞期另有 per-GM advisory lock，
  多把鎖依排序取得避免死鎖。實作見 `db/bookings.py` 的 `_lock_slot` / `_lock_gms`
- 冪等鍵：`UNIQUE (request_id)` + `ON CONFLICT DO NOTHING`，由前端在「按下送出」
  那一刻產生一組 UUID；日期/時間/主持人任一改變就換新的，否則改了日期會被當成同一筆
- 重試：只在應用層、以 `request_id` 為鍵；連線層/pool 層自動重試會製造超賣
- 上線前檢查：所有 serial 欄位驗證 `last_value >= MAX(id)`

## 通知信（Brevo）

五封信，寄的時機與收件人都寫死在規則裡，不需要介面設定：

| 代號 | 主旨前綴 | 什麼時候寄 | 寄給誰 |
|---|---|---|---|
| `gm_assigned` | 【指定通知】 | 某人**這次新被指定**為主持人 | 那位主持人（一人一封） |
| `admin_reviewed` | 【場次確認通知】 | 狀態轉成 `gm_reviewed`（主持人全數確認） | 全部管理員 |
| `player_booked` | 【預約成功】 | 狀態轉成 `booked` | 預定的玩家 |
| `abuse_point` | 【異常行為通知】 | 有人被記點，但**還沒**到停權門檻 | 全部管理員 |
| `abuse_banned` | 【停權通知】 | 有人因記點被自動停權 | 全部管理員 |

跨過停權門檻的那一次**只發 `abuse_banned`**，不發 `abuse_point`——同一個事件
寄兩封講同一件事的信只是製造兩倍的信。

### 設定

只需要一個環境變數。在 Render → Environment 新增：

| 變數 | 必填 | 說明 |
|---|---|---|
| `BREVO_API_KEY` | 是 | Brevo → SMTP & API → API Keys 產生。**沒設定就整個通知功能靜默停用**，其他功能完全不受影響 |

沒設定時本機開發不會誤寄信給真人，這是刻意的預設。

啟動時會印一行說明目前是開還是關，**部署後請確認看到的是哪一行**：

```
[info] 通知信已啟用（Brevo）
[warn] BREVO_API_KEY 未設定，通知信停用——不會寄出任何信件。
```

★ 這一行的存在是因為「沒寄信」是一種**沉默的失敗**：變數名打錯時所有請求
  都照常成功，只是信永遠不會寄出去，而這件事沒有人會回報。

### 改信件內容不必重新部署

主旨與內文放在 `notification_template` 表，站名與網址放在 `settings` 表，
兩張表都用 SQL 改，改完最多 10 分鐘生效（快取 TTL）：

```sql
-- 換網域之後一定要改這個，否則信寄得出去、按鈕卻連到不存在的地方
UPDATE settings SET value = 'https://新網域' WHERE key = 'site_url';

-- 關掉某一封（例如管理員不想收記點通知，但其他信照收）
UPDATE notification_template SET enabled = false WHERE key = 'abuse_point';

-- 改主旨
UPDATE notification_template SET subject = '【新前綴】…' WHERE key = 'player_booked';
```

要恢復某封信的預設內容，把那一列刪掉，下次啟動會從 `db/default_templates.py`
重新塞回來（`ensure()` 只在該列不存在時寫入，不會蓋掉你改過的內容）。

開關有兩層，因為它們回答的是不同的問題：

- `roles.email_notification`——這個**角色**要不要收信（例：主持人一律不收）
- `notification_template.enabled`——這**封**信要不要發（例：記點通知太吵）

### 已知限制

- **進程重啟會掉還沒寄出的信。** 寄送走的是行程內的背景執行緒，不是外部工作
  佇列。Render 重新部署時佇列裡的信會消失。以通知這個用途可以接受（掉一封的
  後果是有人晚點才知道，不是資料錯）；★ 哪天通知變成不可掉的東西（例如拿來寄
  付款連結），這個取捨要重新做
- **Brevo 免費方案每天 300 封**，以現在的量（一天個位數）沒有壓力
- 寄信結果看 `/internal/idle-gap-stats` 的 `notifications` 欄位：
  `sent` / `failed` / `dropped` / `queued`

### 實測過的失效行為（2026-09-02）

寄信的第一原則是「**失敗不能影響主要流程**」。以下每一項都是實際打 API 驗證的，
不是設計意圖：

| 情境 | 結果 |
|---|---|
| Brevo 回 500 | 預約照樣成立（HTTP 200） |
| Brevo 連不上 | 預約照樣成立 |
| Brevo 卡住 30 秒不回應 | 預約照樣成立，且**請求沒有被拖慢**（1.1 秒，與正常時相同） |
| 範本內容寫壞 | 預約照樣成立 |
| 範本整列被刪掉 | 預約照樣成立 |
| 待寄佇列塞滿（上限 200） | 丟棄超出的，佇列不成長，`dispatch()` 不拋例外 |
| 組信途中有一句 SQL 失敗 | 預約照樣成立（**修掉一個真的存在的 bug**，見下） |

做得到這些是因為三件事一起成立：組信在交易裡、送出在 commit **之後**、
實際的 HTTP 呼叫在背景執行緒。順序反過來的話，一筆最後被 rollback 掉的預約
會留下一封「預約成功」的信——而信收不回來。

### 踩到的坑：把例外吞掉，會讓 commit 悄悄變成 rollback

最後一項一開始是**壞的**，而且壞得最難發現。組信要讀範本與收件人，所以跑在
呼叫端的交易裡；而 PostgreSQL 的交易只要有任何一句 SQL 出錯就進入 aborted
狀態，之後 `conn.commit()` **不會拋例外**，Postgres 直接把它當成 ROLLBACK。

所以「用 try/except 把組信的例外吞掉」這個看起來最安全的寫法，實際造成的是：

    組信時一句 SQL 爆掉 → 例外被吞掉 → commit 悄悄變成 rollback
    → API 回 201「預約成功」→ 而那筆預約根本不存在

使用者收不到任何錯誤訊息，只會在之後某天發現場次不見了。

實測方式是讓 `_gm_names()` 裡的查詢故意失敗，然後檢查同一筆交易中**先寫進去**
的欄位還在不在——結果是不在。修法是把每一支組信函式包進 SAVEPOINT
（`notify_events.safe`），失敗就 `ROLLBACK TO SAVEPOINT`，交易回到組信之前的
乾淨狀態。同一個測試改完之後寫入就保住了。

★ 這個坑不限於通知信：**任何在別人的交易裡跑、又不想影響對方的旁支邏輯**
  （記 log、寫統計、發事件）都有一樣的問題，光包 try/except 是不夠的。
