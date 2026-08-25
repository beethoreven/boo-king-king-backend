"""
全站資料庫 schema 建立/遷移，集中在這一支檔案，不要分散在各個模組裡。

建立順序有相依性，不能隨便調換：
1. users（帳號）——booking_record.player_id、gm{n}_user_id 會參照這裡
2. mms（可提供服務的劇本）——booking_record.mms_id 會參照這裡
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

        _ensure_users(cur)
        _ensure_mms(cur)
        _ensure_booking_record(cur)
        _drop_legacy_mms_gm(cur)

        _schema_ready = True


def _drop_legacy_mms_gm(cur) -> None:
    """移除舊的 mms_gm 資料表。

    早期版本把「哪些主持人會哪個角色」拆成獨立的 junction table，後來改回
    mms.gm{n}_user 陣列（理由見 _ensure_mms）。這行讓已經建過那張表的資料庫
    跟著清乾淨，對全新的資料庫是無害的空操作。

    確認沒有其他東西依賴它之後才刪，所以不需要 CASCADE——如果哪天真的有
    外部依賴，讓它報錯比靜靜連帶刪掉安全。
    """
    cur.execute("DROP TABLE IF EXISTS mms_gm")


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
            role INTEGER NOT NULL CHECK (role IN (1, 2, 3)),
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


def _ensure_mms(cur) -> None:
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
    寫成 CHECK 約束即可（見下方 _ensure_mms_gm_constraints）；
    分成兩張表時這條規則跨表，只能靠應用層自律。
    """
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mms (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,

            -- 遊戲時長（小時），允許小數點以下一位，例如 3.5
            period NUMERIC(3, 1),

            -- 金額與訂金，都是整數（新台幣元，不處理角）
            price INTEGER,
            booking_cost INTEGER,

            -- 遊戲人數。純顯示用字串，因為可能是「6-8 人」這種浮動描述，
            -- 不要改成數字型別——它不參與任何計算或比對。
            players TEXT,

            -- 每個場次時段最多可以排幾組候補（不含已成立的那一組）。
            -- 放在 mms 而不是全域設定，因為熱門劇本可能想放寬、冷門的想收緊。
            -- 要改成全站統一調整的話，改動這一欄的預設值即可。
            waitlist_limit INTEGER NOT NULL DEFAULT 3 CHECK (waitlist_limit >= 0),

            -- 主持人角色名稱。NULL 代表這個劇本沒有這個角色。
            gm1_name TEXT,
            gm2_name TEXT,
            gm3_name TEXT,
            gm4_name TEXT,

            -- 會這個角色的主持人（users.id 的集合）。空陣列代表還沒指派。
            gm1_user INTEGER[] NOT NULL DEFAULT '{}',
            gm2_user INTEGER[] NOT NULL DEFAULT '{}',
            gm3_user INTEGER[] NOT NULL DEFAULT '{}',
            gm4_user INTEGER[] NOT NULL DEFAULT '{}',

            status TEXT NOT NULL DEFAULT 'active'
        )
        """
    )
    # 既有資料庫補欄位（全新建立的已經在上面的 CREATE TABLE 裡了）
    existing = _existing_columns(cur, "mms")
    for n in (1, 2, 3, 4):
        if f"gm{n}_user" not in existing:
            cur.execute(
                f"ALTER TABLE mms ADD COLUMN gm{n}_user INTEGER[] NOT NULL DEFAULT '{{}}'"
            )

    _ensure_mms_gm_constraints(cur)


def _ensure_mms_gm_constraints(cur) -> None:
    """強制「沒有這個角色，就不能有人被指派到這個角色」。

    gm{n}_name 為 NULL（劇本沒有這個角色）時，gm{n}_user 必須是空陣列。
    這條規則在 gm{n}_user 還是獨立資料表的時候跨兩張表、寫不成 CHECK；
    現在兩個欄位同表，資料庫可以自己擋下矛盾的資料。

    先 DROP 再 ADD 是為了冪等——同一個 process 重跑不會因為約束已存在而失敗，
    改了條件內容之後也會換成新版本。
    """
    for n in (1, 2, 3, 4):
        cur.execute(f"ALTER TABLE mms DROP CONSTRAINT IF EXISTS gm{n}_slot_consistent")
        cur.execute(
            f"""
            ALTER TABLE mms ADD CONSTRAINT gm{n}_slot_consistent
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
        gm_confirm   等待主持人確認
        gm_reviewed  主持人已審閱
        booked       已成立
        cancelled    已取消
    （原始需求寫的是 gm_comfirm，是 confirm 的拼字錯誤，這裡用正確拼法。）

    候補順位刻意「不存欄位、每次由 created_at 算出來」：

        SELECT id, player_id,
               ROW_NUMBER() OVER (
                   PARTITION BY mms_id, session_date, session_time
                   ORDER BY created_at
               ) AS position
        FROM booking_record
        WHERE mms_id = ? AND session_date = ? AND session_time = ?
          AND status <> 'cancelled'

    position = 1 是成立的那一組，其餘依序是候補。

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

    候補上限的檢查（這是真正需要小心的地方）：
    「這個時段還沒滿」這件事無法用唯一約束表達——要擋的是「第 N+1 筆」，
    而那筆資料在檢查當下還不存在，沒有東西可以鎖（典型的 phantom read）。
    正確做法是在交易一開始先取得該時段的 advisory lock：

        SELECT pg_advisory_xact_lock(
            hashtext(mms_id::text || '|' || session_date::text || '|' || session_time::text)
        );

    這把鎖只序列化「同一個時段」的預約，不影響其他時段，並且在交易結束時
    自動釋放（xact 版本不需要手動解鎖，交易回滾也會放掉）。拿到鎖之後再
    COUNT 現有未取消的筆數、跟 mms.waitlist_limit 比較，才 INSERT。

    刻意沒有對 (mms_id, session_date, session_time) 建唯一約束——這一點
    很容易被後人誤判成漏了約束而「補上」，補了會直接讓候補功能失效，
    所以說清楚：

    一個劇本在同一個時段**只會開一場遊戲**，這一點沒有例外。但這張表
    允許同一個 (劇本, 日期, 時間) 存在多筆資料，因為那多筆代表的是
    「1 組正選 + N 組候補」對同一場的排隊，不是多場遊戲並行。
    唯一性語意在「同一時段只有 position = 1 那一組會真的開場」這件事上，
    由上面的上限檢查與順位計算負責，不是由資料庫的唯一約束負責。
    """
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS booking_record (
            id SERIAL PRIMARY KEY,

            mms_id INTEGER NOT NULL REFERENCES mms(id),
            player_id INTEGER NOT NULL REFERENCES users(id),

            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            session_date DATE NOT NULL,
            session_time TIME NOT NULL,

            status TEXT NOT NULL DEFAULT 'gm_confirm',

            -- 實際支付的訂金金額。與 mms.booking_cost 分開存，因為後者是
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

            -- 這場實際由誰主持，對應 mms.gm{n}_name 的四個角色。
            -- NULL 代表該劇本沒有這個角色，或尚未指定。
            gm1_user_id INTEGER REFERENCES users(id),
            gm2_user_id INTEGER REFERENCES users(id),
            gm3_user_id INTEGER REFERENCES users(id),
            gm4_user_id INTEGER REFERENCES users(id),

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
    # 場次查詢與候補順位計算共用這一個索引。結尾帶 created_at 是刻意的：
    # 上面那段 ROW_NUMBER 的 ORDER BY 就是 created_at，索引順序對上之後
    # 排序可以直接走索引，不需要額外的 sort 步驟。
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_booking_slot "
        "ON booking_record (mms_id, session_date, session_time, created_at)"
    )
    # 某玩家的預約紀錄
    cur.execute("CREATE INDEX IF NOT EXISTS idx_booking_player ON booking_record (player_id)")
