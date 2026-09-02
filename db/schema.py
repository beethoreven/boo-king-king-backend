"""
全站資料庫 schema 建立/遷移，集中在這一支檔案，不要分散在各個模組裡。

建立順序有相依性，不能隨便調換：
1. users（帳號）——booking_record.player_id、gm{n}_user_id 會參照這裡
2. mmg（可提供服務的劇本）——booking_record.mmg_id 會參照這裡
3. booking_record（預定場次）

只支援 Postgres（Neon）。冪等操作（CREATE TABLE IF NOT EXISTS、補欄位前
先檢查），但只需要在這個 process 生命週期裡成功跑過一次——第一次成功後
就把旗標打開，之後直接跳過。理由與 zh-cn-to-tw 相同：Neon 這種每次連線
都有實質延遲的服務，重跑整段檢查的成本不能忽略。

補欄位那幾處是「先查 information_schema 再 ALTER TABLE ADD COLUMN」，
中間有 TOCTOU 空隙，用 _schema_lock 保護。這把鎖只鎖「跑不跑這段初始化」
這個判斷本身，一次性、跑完就不會再進來，不是包住每次資料庫存取的那種鎖
（那種鎖的問題見 db/connection.py 開頭的說明）。
"""

from __future__ import annotations

import threading

from db import default_templates

_schema_ready = False
_schema_lock = threading.Lock()


def _existing_columns(cur, table: str) -> set[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)
    )
    return {row[0] for row in cur.fetchall()}


def ensure_schema(cur) -> None:
    global _schema_ready
    if _schema_ready:
        return

    with _schema_lock:
        # 鎖內重新檢查：等鎖的期間可能已經有另一個執行緒跑完了
        # （double-checked locking），不重檢會把補欄位那幾段再跑一次。
        if _schema_ready:
            return

        # ★ 上面那把是 threading.Lock，只鎖得住「同一個 process 裡的執行緒」。
        #   正式環境用 gunicorn 跑多個 worker，那是多個**行程**——它們會同時
        #   進到這裡，同時對同一張表下 ALTER TABLE，互相搶 AccessExclusiveLock
        #   而死結，兩個 worker 一起啟動失敗，服務根本起不來。
        #
        #   實測過：`gunicorn --workers 2` 直接噴
        #   `psycopg.errors.DeadlockDetected` 然後 "Worker failed to boot"。
        #   單一 process 的 Flask 開發伺服器永遠測不出這件事。
        #
        #   所以要再加一把**跨行程**的鎖。advisory lock 是交易層級的，會在
        #   呼叫端 commit 時自動釋放；第二個 worker 會擋在這一行，等第一個
        #   建完才進去，然後發現該有的都有了（這裡的每一段本來就是冪等的）。
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('booking-king:ensure_schema'))")

        _ensure_settings(cur)
        _ensure_notification_template(cur)
        default_templates.ensure(cur)
        # roles 要在 users 之前——users.role 有外鍵指過來。
        _ensure_roles(cur)
        _ensure_users(cur)
        # rooms 要在 mmg 之前——mmg.room_id 參照它。
        _ensure_rooms(cur)
        _ensure_mmg(cur)
        # 排期兩張表都參照 mmg(id)，必須排在 _ensure_mmg 之後。
        _ensure_schedule(cur)
        _ensure_booking_record(cur)
        _ensure_sessions(cur)
        _ensure_abuse_log(cur)
        _ensure_abuse_rule(cur)

        _schema_ready = True


def _ensure_sessions(cur) -> None:
    """應用程式自己簽發的登入 session。

    這張表存在的理由，是把「Google 證明了你的身分」跟「你這次登入還有效」
    這兩件事分開。Google ID Token 大約一小時就過期，那是它的設計目的——
    它要證明的是「此刻這確實是一個 Google 認證過的身分」，不是「這個人接下來
    整段時間都有權限」。若拿 ID Token 當每次 API 呼叫的憑證，使用者會在
    登入約一小時後被無預警登出，而且所有手動測試都測不出來（測試流程通常
    幾分鐘就結束）。

    對預約系統來說，最糟的觸發時機是玩家填完資料、按下送出的那一刻。

    token 是 secrets.token_hex(32) 產生的隨機字串，不是 JWT——前端不需要
    解析它的內容，只需要原樣帶回來。不可解析反而是優點：沒有人會想從裡面
    讀權限資訊，權限一律當場查資料庫。

    有效期是「滑動窗口」：每次成功查驗都更新 last_seen_at，只有連續
    SESSION_IDLE_DAYS 天完全沒有任何請求才過期，不是固定時鐘到點就失效。
    這讓「不要在使用中途登出使用者」成為結構上的保證，而不是靠額外補丁。
    """
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    # 「把某個使用者的所有 session 一次撤銷」會用到（停用帳號時）
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_email ON sessions (email)")


def _ensure_abuse_log(cur) -> None:
    """異常請求的紀錄。

    存兩種事件：
      bad_request —— 格式層的參數錯誤（start=abc、未知頁籤、日期格式壞掉）。
                     前端送不出這種東西，所以這是「有人在手動戳 API」的
                     強訊號。
      rate_limit  —— 單一帳號請求量超過門檻。訊號很弱：重整、網路重試、
                     開賣尖峰都會觸發，所以只記錄、不作為停權依據。

    ★ 為什麼不是在 users 上加一個計數欄位：計數只會告訴你「有幾次」，
      而事後真正要判斷的是「什麼時候、打哪一支、送了什麼」。少了那些，
      看到一個數字 47 也不知道該不該停權。

    ★ 同一位使用者、同一種事件在短時間內連續發生時，累加 hits 而不是
      一直插新列——被打爆的時候，寫入紀錄本身不該變成第二個壓力來源。
      合併的窗口見 auth_utils/guard.py 的 MERGE_WINDOW_SECONDS。
    """
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS abuse_log (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            kind TEXT NOT NULL,
            path TEXT,
            detail TEXT,
            hits INTEGER NOT NULL DEFAULT 1,
            first_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    # 查詢一律是「某人最近發生什麼」或「最近全站發生什麼」，兩者都靠這個索引。
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_abuse_log_user_time "
        "ON abuse_log (user_id, last_at DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_abuse_log_time ON abuse_log (last_at DESC)"
    )


def _ensure_settings(cur) -> None:
    """每家店會不一樣的那幾個值。

    跨店家的架構是「一份程式碼、每店一套獨立的基礎設施」，而店與店的差異
    只放兩個地方：Render 的環境變數，以及這張表。環境變數放的是連線資訊
    （資料庫、金鑰），這裡放的是**會出現在使用者眼前的東西**——店名、網址、
    寄件人。分界線是「改了要不要重新部署」：這張表不用。

    ★ 沒有介面，用 SQL 改（同 abuse_rule、roles）：

        UPDATE settings SET value = '南極圈' WHERE key = 'site_name';

    ★ site_url 特別容易忘記改。它是通知信裡那顆按鈕的目標，換網域之後
      如果沒跟著改，信寄得出去、按鈕卻連到一個不存在的地方——而寄信本身
      不會失敗，所以不會有任何錯誤讓你發現。
    """
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    cur.execute(
        """
        INSERT INTO settings (key, value) VALUES
            ('site_name',    '步經徑'),
            ('site_url',     'https://booking-king.tutor-arthur.workers.dev'),
            ('sender_name',  '步經徑'),
            ('sender_email', 'beethoreven@gmail.com')
        ON CONFLICT (key) DO NOTHING
        """
    )


def _ensure_notification_template(cur) -> None:
    """通知信的主旨與內容。

    ## 為什麼放資料庫而不是程式碼或 Brevo

    放**程式碼**：改一個字要重新部署，而且每家店不能不同。
    放 **Brevo 的範本編輯器**：有視覺編輯器，但內容就住在 Brevo——沒有版本
      控制、換供應商帶不走，而且每家店要各自建一套範本，那會抵銷掉
      「email 不必每店開帳號」這個優勢。
    放**這裡**：跟著那家店自己的資料庫走，符合既有的跨店家決定；換供應商
      不影響；改字是一行 SQL。代價是沒有視覺編輯器——但這些是短通知，
      不是行銷信，用不到拖拉排版。

    ## 內容裡的變數

    用 {{name}} 這種雙大括號。代入時**值會做 HTML 逃逸**（見
    notifications.py 的 render），所以劇本名或玩家暱稱裡有 < 或 & 不會
    弄壞版面，也不會變成可以注入的地方。

    ## enabled

    單封關掉用的。roles 那邊的開關是「這個**角色**要不要收信」，這裡是
    「這**封**信要不要發」——兩個維度，例如「管理員要收信，但不要收記點
    通知」就是把這一封關掉。
    """
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS notification_template (
            key       TEXT PRIMARY KEY,
            subject   TEXT NOT NULL,
            body_html TEXT NOT NULL,
            enabled   BOOLEAN NOT NULL DEFAULT true
        )
        """
    )


def _ensure_roles(cur) -> None:
    """角色，以及每個角色要不要收通知。

    ## 為什麼是一張表而不是三個常數

    role 的名稱原本散在四個地方各有一份（後端的 ROLE_LABEL、schema 的
    CHECK、seed_demo、前端的下拉選項）。改一個地方就要記得改另外三個，
    而漏掉的那一個不會報錯，只會在某個畫面上顯示不一樣的字。

    現在這裡是唯一的來源：名稱從這裡讀，users.role 用外鍵指過來。

    ## 通知開關

    line_notification / email_notification 決定「這個角色的人要不要收
    通知」，各自對應一種情境：

        管理員    所有指定的主持人都確認完了 → 通知管理員去發付訂金通知
        主持人    玩家指定了他 → 通知他去點確認
        一般玩家  狀態切到 booked → 通知他預約成立

    ★ 沒有介面，用 SQL 改就好（跟 abuse_rule 同一個理由——兩三行資料，
      做介面的成本遠大於它省下的事）：

        UPDATE roles SET email_notification = true WHERE id = 2;
        UPDATE roles SET line_notification = false;   -- 全部關掉

    ★ 兩個都 false 就是這個角色完全不收通知，那是合法的設定，不是壞掉。

    ★ line_notification 目前**只有欄位，還沒有實作**。LINE 官方帳號的免費
      額度是每月 200 則，而這套流程每筆預約約 5-6 則（發給幾個人算幾則，
      同一個人下次再發還是算），一個月三十幾筆就用完，所以先走 email
      （Brevo 免費 300 封／天）。欄位先建著，等真的要接 LINE 時不用再動
      schema。
    """
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS roles (
            id                 INTEGER PRIMARY KEY,
            name               TEXT NOT NULL,
            line_notification  BOOLEAN NOT NULL DEFAULT false,
            email_notification BOOLEAN NOT NULL DEFAULT false
        )
        """
    )
    # ON CONFLICT DO NOTHING：只在沒有時給預設值，不覆蓋案主調過的設定。
    cur.execute(
        """
        INSERT INTO roles (id, name, line_notification, email_notification) VALUES
            (1, '管理員',   false, true),
            (2, '主持人',   false, true),
            (3, '一般玩家', false, true)
        ON CONFLICT (id) DO NOTHING
        """
    )


def _ensure_users(cur) -> None:
    """帳號。

    role 是數字階層，數字越小權限越高：
        1 = 管理員（含 2、3 的權限）
        2 = 主持人（含 3 的權限）
        3 = 一般玩家
    因為是階層而非互斥的角色，權限判斷一律寫成 `user.role <= 需要的等級`，
    不要寫成 `user.role == 1`——後者會讓管理員在主持人專用的功能上被擋下來。

    這裡刻意不建 permissions 表、role 也不設 FK：zh-cn-to-tw 的 permissions
    表存在的理由是「每個角色有不同的 API 用量額度」，本專案沒有這個需求，
    多一張只為了三個固定數字而存在的表沒有意義。用 CHECK 約束把值鎖死即可。
    """
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            name TEXT,
            role INTEGER NOT NULL REFERENCES roles(id),
            status TEXT NOT NULL DEFAULT 'active',

            -- 聯絡方式。兩者都可為空——玩家可能只留其中一種，
            -- 主持人要聯絡時挑有值的那個用。
            -- 刻意用 TEXT 不做格式驗證：LINE ID 沒有固定格式，
            -- 電話也可能是市話、分機、或帶國碼的寫法，在資料庫層
            -- 卡格式只會擋掉合法但沒預期到的輸入。
            line_id TEXT,
            phone TEXT
        )
        """
    )

    # ── 異常記點與累進停權（2026-09-01 加）────────────────────
    #
    # abuse_point   累計點數。每產生一列**新的** abuse_log 就加分
    #               （bad_request 2 分、rate_limit 1 分），每週的排程統一減 1。
    # banned_until  停權到什麼時候。只有 status = 'banned' 時有意義。
    #
    # ★ 為什麼要 banned_until，而不是只有一個 status：停權是有期限的
    #   （5 分鐘／24 小時／30 天），光有狀態就得有人在到期那一刻去解除它。
    #   存下到期時間之後，判定時當場算就好——跟劇本上架用的是同一招，
    #   不需要一個每分鐘醒來的排程（理由見 db/listing.py）。
    for ddl in (
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS abuse_point INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS banned_until TIMESTAMPTZ",
    ):
        cur.execute(ddl)

    # ── role 從 CHECK 換成外鍵（2026-09-01）────────────────────
    #
    # 舊的 CHECK (role IN (1,2,3)) 把合法的角色寫死在 schema 裡，而角色現在
    # 是一張表。留著 CHECK 的話，往 roles 加一列新角色會被它擋下來——一個
    # 「資料表說可以、schema 說不行」的矛盾。
    #
    # 外鍵給的保證更強也更誠實：role 一定指得到 roles 裡真實存在的一列。
    cur.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check")
    cur.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'users_role_fkey'
            ) THEN
                ALTER TABLE users
                    ADD CONSTRAINT users_role_fkey
                    FOREIGN KEY (role) REFERENCES roles(id);
            END IF;
        END $$
        """
    )


def _ensure_abuse_rule(cur) -> None:
    """幾點停權多久。

    做成資料表而不是程式裡的常數，因為這幾個數字要靠實際運作調整，而改常數
    要重新部署。案主直接用 SQL 改，不做管理介面——三行資料，做一個介面的
    成本遠大於它省下來的事。

        UPDATE abuse_rule SET ban_seconds = 600 WHERE min_point = 3;
        INSERT INTO abuse_rule (min_point, ban_seconds, label)
             VALUES (9, 0, 'permanent');     -- 0 = 無限期，要管理員手動解

    判定時取「min_point <= 目前點數」之中 min_point 最大的那一條，所以要加
    中間級距不必動任何程式。
    """
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS abuse_rule (
            min_point   INTEGER PRIMARY KEY,
            ban_seconds INTEGER NOT NULL,
            label       TEXT
        )
        """
    )
    # ON CONFLICT DO NOTHING：這是「沒有才給預設值」，不是「每次啟動都
    # 覆蓋回預設值」——後者會把案主調過的數字默默蓋掉。
    cur.execute(
        """
        INSERT INTO abuse_rule (min_point, ban_seconds, label) VALUES
            (3, 300,     'lower_abuse'),
            (5, 86400,   'middle_abuse'),
            (7, 2592000, 'high_abuse')
        ON CONFLICT (min_point) DO NOTHING
        """
    )


def _ensure_rooms(cur) -> None:
    """包廂。

    場次衝突的真正對象是「包廂被佔用」，不是「同一個劇本被排兩場」——
    同一間包廂裡，10:00 開演的 12 小時本會做到 22:00，20:00 就不能再開
    另一齣戲，即使那是完全不同的劇本。沒有這張表的話這件事表達不出來，
    只能退而求其次比對「同劇本」，而那會漏掉所有跨劇本的重疊。

    先只有一間。欄位也只有 id 與名字：現階段不需要容納人數、設備之類的
    屬性，那些等真的有多間、而且需要據此篩選時再加。
    """
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS rooms (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL
        )
        """
    )
    # 至少要有一間，否則 mmg.room_id 的預設值 1 會指向不存在的列。
    cur.execute("SELECT COUNT(*) FROM rooms")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO rooms (id, name) VALUES (1, %s)", ("包廂",))
        # 手動指定 id 不會推進序列，下一次 INSERT 會撞號。
        cur.execute("SELECT setval('rooms_id_seq', (SELECT MAX(id) FROM rooms))")


def _ensure_mmg(cur) -> None:
    """可提供服務的劇本。

    gm{n}_name 是「這個劇本需要幾個主持人角色、各叫什麼名字」，
    為空代表這個劇本沒有這個角色（例如只需要一位主持人的劇本，
    gm2_name ~ gm4_name 全為 NULL）。

    gm{n}_user 是「有哪些主持人會這個角色」，用 INTEGER[] 存一組 users.id。
    同一個角色可以有好幾位主持人都會，玩家預約時從中挑一位——每一場遊戲
    只會有一位主持人，不是共同擔任。

    為什麼用陣列而不是 junction table：
    陣列元素沒辦法建外鍵，理論上會有「指向不存在使用者」的風險，但這個
    專案不會真的刪除帳號——停用只是把 status 改掉並清空個資，users 那一列
    永遠留著，id 也就永遠有效。既然孤兒 id 不會發生，junction table 換來的
    保護是空的，只剩多一張表的理解成本。

    陣列反而讓一條原本表達不出來的規則變得可以強制：
    「gm{n}_name 為空時，gm{n}_user 必然為空」。這兩個欄位現在同表，
    寫成 CHECK 約束即可（見下方 _ensure_mmg_gm_constraints）；
    分成兩張表時這條規則跨表，只能靠應用層自律。
    """
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mmg (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,

            -- 劇本簡介的外部連結。有值時，畫面上的劇本名稱會變成可以點的
            -- 連結（開新分頁）；沒有值就是純文字。
            --
            -- 簡介本身刻意不放在這個系統裡：那是會不斷變動、圖文並茂的
            -- 行銷內容，塞進來會讓這個資料庫變成一個它不該扮演的角色，
            -- 而且免費方案的容量要留給真正的營運資料。
            url TEXT,

            -- 這齣戲在哪一間包廂演。場次衝突比對的是包廂的佔用，不是
            -- 劇本本身——見 _ensure_rooms 的說明。
            room_id INTEGER NOT NULL DEFAULT 1 REFERENCES rooms(id),

            -- 遊戲時長（小時），允許小數點以下一位，例如 3.5
            period NUMERIC(3, 1),

            -- 開演前的場地佈置時間、散場後的還原時間，單位與精度同 period。
            -- 這兩段時間包廂一樣是被佔著的，所以一場戲實際佔用的區間是
            --   [開始 - ready_time_cost, 開始 + period + reset_time_cost]
            -- 衝突判定比的就是這個區間，不是 period 本身（見 db/bookings.py
            -- 的 has_room_conflict）。
            ready_time_cost NUMERIC(3, 1) NOT NULL DEFAULT 0.5,
            reset_time_cost NUMERIC(3, 1) NOT NULL DEFAULT 0.5,

            -- 金額與訂金，都是整數（新台幣元，不處理角）
            price INTEGER,
            booking_cost INTEGER,

            -- 遊戲人數。純顯示用字串，因為可能是「6-8 人」這種浮動描述，
            -- 不要改成數字型別——它不參與任何計算或比對。
            players TEXT,

            -- 每個場次時段總共可以排幾組。設 3 就是最多 3 組，第 4 組
            -- 會被擋下——不是「第 1 序位之外還能排 3 組」。
            --
            -- 計數以「沒有取消」為準，已成立的那一組同樣佔一個位子：
            -- 容量要擋的是這個時段總共排了幾組人，一組預約不會因為成立了
            -- 就不佔位。
            --
            -- 欄位名沿用 waitlist_limit——它是資料庫識別碼，改名要遷移，
            -- 而使用者看不到欄位名；顯示層一律用「第 N 序位」。
            -- 放在 mmg 而不是全域設定，因為熱門劇本可能想放寬、冷門的想收緊。
            -- 要改成全站統一調整的話，改動這一欄的預設值即可。
            waitlist_limit INTEGER NOT NULL DEFAULT 3 CHECK (waitlist_limit >= 0),

            -- 上架／下架日期。排期判斷的第 0 層，也是唯一一層「絕對外框」：
            -- 區間外一律不開放，特定日期例外也翻不過來（下架代表這齣戲
            -- 不賣了，若要再開一場就是把區間拉長，而不是從例外戳一個洞——
            -- 否則「這齣戲什麼時候有場」要同時看兩個地方才答得出來）。
            --
            -- ★ start_booking 不可為空，預設是建立的當下。
            --
            --   一齣連開放預約時間都掛不出來的劇本，代表它還沒有真的要賣，
            --   那它就不該是可以被預定的。允許留空的話，「先把資料建進去，
            --   哪天再說」的劇本會一直躺在玩家選得到的清單裡。
            --
            --   ★ 是 TIMESTAMP 不是 DATE：上架是一個明確的時刻。同一天
            --     14:00 上架的戲，當天 10:00 的場次不該訂得到、19:00 的
            --     該訂得到——只存日期就表達不出這件事，整個上架日會一次
            --     全開。裸值一律以台北時間解讀，與 session_date/time 同一
            --     個約定（見本檔案 _ensure_booking_record 的說明）。
            start_booking TIMESTAMP NOT NULL
                DEFAULT (NOW() AT TIME ZONE 'Asia/Taipei'),

            -- 下架時刻。可以留空＝不打算下架，那是常態，所以這一欄允許
            -- NULL 而 start_booking 不允許。判斷式寫成
            --   end_booking IS NULL OR <場次時刻> < end_booking
            -- 所以填一個很遠的真實時刻（2999-12-31 00:00）跟填 NULL 結果
            -- 完全相同——★ 不要把遙遠日期當成 magic value 特別處理，
            -- 案主明確要求保留「寫真實數字也 OK」的空間。
            --
            -- 與 start_booking 同樣是 TIMESTAMP：下架也是一個明確的時刻，
            -- 而且時間到了要自動把狀態切成 inactive（見 db/listing.py）。
            end_booking TIMESTAMP,

            -- 上下架時刻不能顛倒。應用層會先擋並回一句看得懂的話，這裡是
            -- 最後一道：一個區間的結束早於開始，代表它從來不曾開放過，
            -- 那不是一個有意義的設定，是一筆壞資料。
            CONSTRAINT mmg_listing_order CHECK (
                end_booking IS NULL OR end_booking > start_booking
            ),

            -- 每個主持人角色是「名稱 + 會這個角色的人」一組，刻意讓
            -- gm{n}_name 與 gm{n}_user 相鄰，直接看資料表時一眼就能
            -- 把角色跟人對起來，不用左右來回捲。
            --
            -- gm{n}_name 為 NULL 代表這個劇本沒有這個角色；
            -- gm{n}_user 是 users.id 的集合，空陣列代表還沒指派。
            gm1_name TEXT,
            gm1_user INTEGER[] NOT NULL DEFAULT '{}',
            gm2_name TEXT,
            gm2_user INTEGER[] NOT NULL DEFAULT '{}',
            gm3_name TEXT,
            gm3_user INTEGER[] NOT NULL DEFAULT '{}',
            gm4_name TEXT,
            gm4_user INTEGER[] NOT NULL DEFAULT '{}',

            status TEXT NOT NULL DEFAULT 'active'
        )
        """
    )
    # 既有資料庫補欄位。注意這條路徑補出來的欄位會排在最後面，
    # 拿不到上面 CREATE TABLE 的相鄰順序——Postgres 沒有調整既有欄位
    # 順序的語法，只能重建整張表。要讓現有資料庫也變成相鄰排列，
    # 跑一次 db/migrate_reorder_mms.py。
    existing = _existing_columns(cur, "mmg")
    for col in ("ready_time_cost", "reset_time_cost"):
        if col not in existing:
            # 既有資料一律 0.5 小時。DEFAULT 會把現有的列一起填上，
            # 不需要另外 UPDATE。
            cur.execute(
                f"ALTER TABLE mmg ADD COLUMN {col} NUMERIC(3, 1) NOT NULL DEFAULT 0.5"
            )
    if "url" not in existing:
        cur.execute("ALTER TABLE mmg ADD COLUMN url TEXT")
    for col in ("start_booking", "end_booking"):
        if col not in existing:
            cur.execute(f"ALTER TABLE mmg ADD COLUMN {col} DATE")
    # start_booking 補成 NOT NULL。分三步而不是一步 ALTER：既有的列可能
    # 已經是 NULL（這個欄位剛加進來的那一版允許留空），直接 SET NOT NULL
    # 會失敗。先補值再上約束。
    #
    # 既有劇本補「今天」而不是某個很早的日期：它們本來就沒有上架日的概念，
    # 挑今天代表「從現在起可以賣」，而不是假裝它從 2020 年就上架了。
    cur.execute(
        "UPDATE mmg SET start_booking = (NOW() AT TIME ZONE 'Asia/Taipei')::date "
        "WHERE start_booking IS NULL"
    )
    # DATE → TIMESTAMP。舊值是日期，轉過來就是那天 00:00，語意不變
    # （原本「那一天整天都開放」＝「從那天 00:00 起開放」）。
    # 預設值要先拆掉再改型別，不然 Postgres 會拒絕轉換舊的 DATE 預設。
    cur.execute("ALTER TABLE mmg ALTER COLUMN start_booking DROP DEFAULT")
    cur.execute(
        "ALTER TABLE mmg ALTER COLUMN start_booking TYPE TIMESTAMP "
        "USING start_booking::timestamp"
    )
    cur.execute(
        "ALTER TABLE mmg ALTER COLUMN start_booking SET DEFAULT "
        "(NOW() AT TIME ZONE 'Asia/Taipei')"
    )
    cur.execute("ALTER TABLE mmg ALTER COLUMN start_booking SET NOT NULL")
    cur.execute(
        "ALTER TABLE mmg ALTER COLUMN end_booking TYPE TIMESTAMP "
        "USING end_booking::timestamp"
    )
    # 既有資料可能已經有顛倒的區間（上一版沒有這個約束），所以先清乾淨
    # 再上約束，否則 ADD CONSTRAINT 會失敗、整個 ensure_schema 起不來。
    cur.execute(
        "UPDATE mmg SET end_booking = NULL "
        "WHERE end_booking IS NOT NULL AND end_booking <= start_booking"
    )
    cur.execute("ALTER TABLE mmg DROP CONSTRAINT IF EXISTS mmg_listing_order")
    cur.execute(
        "ALTER TABLE mmg ADD CONSTRAINT mmg_listing_order CHECK ("
        "end_booking IS NULL OR end_booking > start_booking)"
    )
    if "room_id" not in existing:
        # 既有資料一律歸到第一間包廂：導入這個欄位之前只有一間，
        # 所有劇本本來就都在那裡演。
        cur.execute(
            "ALTER TABLE mmg ADD COLUMN room_id INTEGER NOT NULL DEFAULT 1 "
            "REFERENCES rooms(id)"
        )
    for n in (1, 2, 3, 4):
        if f"gm{n}_user" not in existing:
            cur.execute(
                f"ALTER TABLE mmg ADD COLUMN gm{n}_user INTEGER[] NOT NULL DEFAULT '{{}}'"
            )

    _ensure_mmg_gm_constraints(cur)


def _ensure_schedule(cur) -> None:
    """排期的第 1、2 層。第 0 層（上下架日期）在 mmg 上，見 _ensure_mmg。

    ## 為什麼是兩張表而不是一張，也不是欄位

    第 2 層「每週常態」最多 7 列（週日到週六各一列），不隨時間增長：
    某個星期沒開，就是**沒有那一列**，不是存一個空陣列。這讓「常態全關、
    只靠指定日期開」不需要任何額外旗標——沒有任何一列就是那個意思。

    時段跟著星期走而不是跟著劇本走，是因為「平日只開 19:00、週末開
    10:00/14:00/19:00」是這一行最常見的形狀。把時段放在 mmg 上變成
    「星期 × 時段」的交叉相乘，就表達不出這件事，只能一天一天設特例補，
    一年就是一百多筆。

    第 1 層「特定日期例外」則是真的會一直長，所以獨立成表：
      is_open=false → 那天整天不開（臨時公休、GM 請假）
      is_open=true  → 那天開，且時段以這一筆的 times 為準（臨時加場，
                      或當天時段跟平常不一樣）

    ## 為什麼例外的優先序高於常態

    例外是「這一週跟平常不同」，常態是「每週都這樣」。週期性的差異
    （平日／週末）屬於常態層，單週的臨時變動屬於例外層——這是案主定的
    分工，也是這個模型能同時保持簡單與完整的原因。
    """
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mmg_weekly_slot (
            mmg_id INTEGER NOT NULL REFERENCES mmg(id) ON DELETE CASCADE,

            -- 0=週日 … 6=週六，跟 Python 的 date.isoweekday()%7 與
            -- JavaScript 的 Date.getDay() 一致，兩邊都不用換算。
            weekday SMALLINT NOT NULL CHECK (weekday BETWEEN 0 AND 6),

            -- 這個星期的開場時段。空陣列沒有意義（等於沒開），所以擋掉：
            -- 「不開」的表達方式是沒有這一列，不是留一列空的。
            times TIME[] NOT NULL CHECK (array_length(times, 1) >= 1),

            PRIMARY KEY (mmg_id, weekday)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mmg_schedule_exception (
            mmg_id INTEGER NOT NULL REFERENCES mmg(id) ON DELETE CASCADE,
            exception_date DATE NOT NULL,
            is_open BOOLEAN NOT NULL,

            -- is_open=false 時無意義（整天不開，沒有時段可言）；
            -- is_open=true 時是那天實際開放的時段，必須至少有一個，
            -- 否則「開放但沒有任何時段」跟「不開」是同一件事的兩種寫法。
            times TIME[] NOT NULL DEFAULT '{}',
            CHECK (NOT is_open OR array_length(times, 1) >= 1),

            -- 一天最多一筆。同一天同時有「開」跟「關」兩筆的話，哪一筆
            -- 說了算就變成看查詢順序的運氣。
            PRIMARY KEY (mmg_id, exception_date)
        )
        """
    )
    # 日曆一次要撈一個月的例外，走這個索引。
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_mmg_exception_date "
        "ON mmg_schedule_exception (mmg_id, exception_date)"
    )


def _ensure_mmg_gm_constraints(cur) -> None:
    """強制「沒有這個角色，就不能有人被指派到這個角色」。

    gm{n}_name 為 NULL（劇本沒有這個角色）時，gm{n}_user 必須是空陣列。
    這條規則在 gm{n}_user 還是獨立資料表的時候跨兩張表、寫不成 CHECK；
    現在兩個欄位同表，資料庫可以自己擋下矛盾的資料。

    先 DROP 再 ADD 是為了冪等——同一個 process 重跑不會因為約束已存在而失敗，
    改了條件內容之後也會換成新版本。
    """
    for n in (1, 2, 3, 4):
        cur.execute(f"ALTER TABLE mmg DROP CONSTRAINT IF EXISTS gm{n}_slot_consistent")
        cur.execute(
            f"""
            ALTER TABLE mmg ADD CONSTRAINT gm{n}_slot_consistent
            CHECK (gm{n}_name IS NOT NULL OR cardinality(gm{n}_user) = 0)
            """
        )


def _ensure_booking_record(cur) -> None:
    """預定場次。

    時間欄位的型別選擇（這是刻意的，不要改成 TEXT）：

    created_at 用 TIMESTAMPTZ。Postgres 內部一律存 UTC，但型別本身帶時區
    資訊，讀取時用 `AT TIME ZONE 'Asia/Taipei'` 就能得到 +8 的顯示值，
    沒有任何額外負擔。這比「存一個已經 +8 過的裸時間」安全得多——後者
    一旦有跨時區的用戶端或伺服器，就再也分不出某個值到底加過沒有。

    session_date / session_time 則刻意用不帶時區的 DATE / TIME。
    場次時間是「牆上時鐘」的約定（8/30 下午兩點在店裡集合），不是一個
    UTC 瞬間；它不該隨著讀取者的時區而改變顯示。這兩者的區別是真實的，
    不是疏漏：一個是「這筆資料何時被寫入」（瞬間），一個是「大家幾點到」
    （約定）。全系統統一以台北時間解讀 session_* 欄位。

    status 目前的值：
        gm_confirm   等待主持人確認（尚有被指定的主持人還沒按確認）
        gm_reviewed  主持人已審閱（全部被指定的主持人都確認過了）
        booked       已成立（訂金已收足，見 record_deposit()）
        ended        場次已結束
        cancelled    已取消
    （原始需求寫的是 gm_comfirm，是 confirm 的拼字錯誤，這裡用正確拼法。）

    booked → ended 的轉換有兩條路：主持人／管理員手動標記，或是
    mark_ended_batch() 排程（建議每天 00:00 跑一次）自動把
    session_date+session_time+mmg.period 已經過去的 booked 場次批次
    轉掉。刻意不做成「每次查詢就即時判斷已經過了沒有」——那會讓同一筆
    資料的狀態隨著「現在幾點」而在读取當下臨時計算出不同答案，等於
    status 有兩個事實來源（欄位裡存的值，跟查詢當下算出來的值）。
    寫進資料庫的 status 才是唯一事實，批次工作負責讓它跟上時間，
    不是每次讀取都重新猜一次。

    序位刻意「不存欄位、每次由 created_at 算出來」：

        SELECT id, player_id,
               ROW_NUMBER() OVER (
                   PARTITION BY mmg_id, session_date, session_time
                   ORDER BY created_at
               ) AS position
        FROM booking_record
        WHERE mmg_id = ? AND session_date = ? AND session_time = ?
          AND status <> 'cancelled'

    position 就是序位：1 是最先預定的那一組，其餘依序往後。

    為什麼不存一個 priority 欄位（或 waitlist_position 數字）：
    那會讓「誰是第一位」有兩個來源——created_at 的先後，和欄位裡的值——
    而兩者可以不一致。取消時要把下一位升上來，那是「改另一列」的寫入，
    必須跟取消本身在同一個交易裡；只要升位因為任何原因沒跑成功
    （交易回滾、程式 bug、升位當下剛好有新預約插進來），就會留下
    「這個時段沒有任何人是第一位」的狀態，而且看起來跟「全部取消了」
    一模一樣，不會有錯誤訊息。用算的則不可能出現這種狀態：取消就只是
    把自己的 status 改掉，順位自動遞補，沒有第二個地方需要同步。

    效能上也沒有取捨：每個 (劇本, 日期, 時間) 的分組最多只有
    1 + waitlist_limit 列（預設 4 列），對 4 列做 ROW_NUMBER 的成本可以忽略，
    而玩家瀏覽用的熱查詢本來就走 Redis 快取、不會打到這裡。

    容量上限的檢查（這是真正需要小心的地方）：
    「這個時段還沒滿」這件事無法用唯一約束表達——要擋的是「第 N+1 筆」，
    而那筆資料在檢查當下還不存在，沒有東西可以鎖（典型的 phantom read）。
    正確做法是在交易一開始先取得該時段的 advisory lock：

        SELECT pg_advisory_xact_lock(
            hashtext(mmg_id::text || '|' || session_date::text || '|' || session_time::text)
        );

    這把鎖只序列化「同一個時段」的預約，不影響其他時段，並且在交易結束時
    自動釋放（xact 版本不需要手動解鎖，交易回滾也會放掉）。拿到鎖之後再
    COUNT 現有未取消的筆數、跟 mmg.waitlist_limit 比較，才 INSERT。

    刻意沒有對 (mmg_id, session_date, session_time) 建唯一約束——這一點
    很容易被後人誤判成漏了約束而「補上」，補了會直接讓排序功能失效，
    所以說清楚：

    一個劇本在同一個時段**只會開一場遊戲**，這一點沒有例外。但這張表
    允許同一個 (劇本, 日期, 時間) 存在多筆資料，因為那多筆代表的是
    同一場的多個序位在排隊，不是多場遊戲並行。
    唯一性語意在「同一時段只有第 1 序位那一組會真的開場」這件事上，
    由上面的上限檢查與順位計算負責，不是由資料庫的唯一約束負責。
    """
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS booking_record (
            id SERIAL PRIMARY KEY,

            mmg_id INTEGER NOT NULL REFERENCES mmg(id),
            player_id INTEGER NOT NULL REFERENCES users(id),

            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            session_date DATE NOT NULL,
            session_time TIME NOT NULL,

            status TEXT NOT NULL DEFAULT 'gm_confirm',

            -- 實際支付的訂金金額。與 mmg.booking_cost 分開存，因為後者是
            -- 「目前的定價」，會隨時間調整；這裡要的是「這筆預約當下收了多少」，
            -- 事後查帳不能被之後的改價影響。
            deposit INTEGER NOT NULL DEFAULT 0,

            -- 主持人／管理員的備註欄。取消原因與取消時間、改期、
            -- 特殊需求、付款狀況等都寫在這裡。
            --
            -- 刻意不為「取消時間」另開一個 cancelled_at 欄位：那個值只在
            -- status = 'cancelled' 時有意義，其餘時候永遠是 NULL，而且
            -- 一旦有了欄位就會冒出「欄位有值但 status 不是 cancelled」
            -- 這種對不起來的狀態要處理。備註是給人讀的自由文字，
            -- 不參與任何查詢或計算，放在這裡剛好。
            --
            -- 若之後真的需要「查出某段期間內被取消的場次」這類功能，
            -- 那要的是完整的狀態變更歷程（誰、何時、從什麼狀態改成什麼），
            -- 屆時應該另開一張 booking_status_log，而不是在這裡補單一欄位。
            note TEXT,

            -- 這場實際由誰主持，對應 mmg.gm{n}_name 的四個角色，以及
            -- 那位主持人是否已經確認這次指定。刻意讓 gm{n}_confirmed
            -- 緊鄰 gm{n}_user_id，跟 mmg 表 gm{n}_name／gm{n}_user 相鄰
            -- 的排列慣例一致——同一件事的兩個屬性放在一起，不用左右
            -- 對照著找。NULL 的 user_id 代表該劇本沒有這個角色或尚未
            -- 指定，此時對應的 confirmed 恆為 false，沒有意義但無害。
            --
            -- 全部被指定的角色都確認過（confirm_gm() 判斷），才會把
            -- status 從 gm_confirm 轉成 gm_reviewed。「確認」是針對
            -- 「這個人」而不是「這一列資料」——同一位主持人若同時佔了
            -- 這場的兩個角色，一次確認會把他佔的每一格都標記，見
            -- confirm_gm() 的說明。
            gm1_user_id INTEGER REFERENCES users(id),
            gm1_confirmed BOOLEAN NOT NULL DEFAULT false,
            gm2_user_id INTEGER REFERENCES users(id),
            gm2_confirmed BOOLEAN NOT NULL DEFAULT false,
            gm3_user_id INTEGER REFERENCES users(id),
            gm3_confirmed BOOLEAN NOT NULL DEFAULT false,
            gm4_user_id INTEGER REFERENCES users(id),
            gm4_confirmed BOOLEAN NOT NULL DEFAULT false,

            -- 冪等鍵。前端在玩家「按下預約」的當下產生一組 UUID，該次意圖的
            -- 所有重試都沿用同一組。寫入時搭配
            -- `ON CONFLICT (request_id) DO NOTHING RETURNING ...`：
            -- 有回傳代表首次成立，沒回傳代表這次意圖早就成功過，查出原本
            -- 那筆回給使用者（回報成功，不是失敗）。
            --
            -- 沒有這個欄位的話，網路不穩導致的重送會產生第二筆「不同但同樣
            -- 合法」的預約——資料庫層看起來完全正常，沒有任何約束被違反，
            -- 但玩家按一次卻佔了兩個名額。這是唯一性約束擋不住的那類問題：
            -- 它保護的是資料的形狀，不是使用者的意圖。
            --
            -- 對應的重試規則：自動重試只能在應用層、且只能以 request_id 為鍵。
            -- 放在連線層或連線池層的通用重試（攔到錯誤就重跑）會製造重複預約，
            -- 因為那一層無法知道交易到底提交了沒有。
            request_id UUID NOT NULL UNIQUE
        )
        """
    )
    # 場次查詢與序位計算共用這一個索引。結尾帶 created_at 是刻意的：
    # 上面那段 ROW_NUMBER 的 ORDER BY 就是 created_at，索引順序對上之後
    # 排序可以直接走索引，不需要額外的 sort 步驟。
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_booking_slot "
        "ON booking_record (mmg_id, session_date, session_time, created_at)"
    )
    # 某玩家的預約紀錄
    cur.execute("CREATE INDEX IF NOT EXISTS idx_booking_player ON booking_record (player_id)")

    # 既有資料庫補欄位。這裡補出來的欄位會排在最後面，拿不到上面
    # CREATE TABLE 的相鄰順序——正式環境已用
    # db/migrate_add_gm_confirmed.py 重建過一次表，這裡純粹是防呆：
    # 就算漏跑那支腳本，這個專案至少也不會因為缺欄位而整個壞掉。
    existing = _existing_columns(cur, "booking_record")
    for n in (1, 2, 3, 4):
        if f"gm{n}_confirmed" not in existing:
            cur.execute(
                f"ALTER TABLE booking_record ADD COLUMN gm{n}_confirmed "
                f"BOOLEAN NOT NULL DEFAULT false"
            )
