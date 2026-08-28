"""
建立與查詢預約。

這裡實作的是 schema.py 的 _ensure_booking_record docstring 記載的那套流程：
advisory lock → 檢查容量 → 冪等寫入 → 算順位。不要繞過這支直接對
booking_record 下 INSERT，那會跳過容量檢查與冪等保護。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta


class BookingError(Exception):
    """預約無法成立。訊息是給使用者看的。"""


@dataclass
class BookingResult:
    id: int
    position: int          # 這一組在該時段的序位，1 是最先預定的
    already_existed: bool  # True 代表這個 request_id 先前就成功過

    @property
    def is_first(self) -> bool:
        """是不是該時段的第 1 序位。

        刻意不叫 is_confirmed——那個字在這個專案已經被主持人的
        gm{n}_confirmed 用掉了，兩個意思不同的 confirmed 並存會看錯。
        序位是「排在第幾個」，跟審核流程無關。
        """
        return self.position == 1

    @property
    def label(self) -> str:
        return f"第 {self.position} 序位"


def _lock_slot(cur, mmg_id: int, session_date, session_time) -> None:
    """鎖住這個時段，直到交易結束。

    為什麼需要：容量檢查要擋的是「第 N+1 筆」，而那筆在檢查當下還不存在，
    沒有既有的列可以鎖（phantom read）。只有先序列化同一時段的預約，
    「先數再寫」才是安全的。

    用 advisory lock 而不是鎖 mmg 那一列：鎖 mmg 會把該劇本所有日期、
    所有時段的預約全部序列化，開賣尖峰時會把不相干的時段也卡住。
    advisory lock 的鍵是「劇本+日期+時間」，只擋真正在搶同一場的人。

    xact 版本在交易結束時自動釋放，包含 rollback——不需要、也不應該
    手動解鎖，否則某條例外路徑忘了解就會卡死整個時段。

    hashtext 理論上可能碰撞，讓兩個不同時段共用同一把鎖。那只會讓它們
    多序列化一點，不會造成錯誤結果，在這個規模可以接受。
    """
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        (f"booking|{mmg_id}|{session_date}|{session_time}",),
    )


def _lock_hosts(cur, host_ids: list[int]) -> None:
    """鎖住這幾位主持人的行事曆，直到交易結束。

    跟 _lock_slot 是同一個 TOCTOU 問題（查沒衝突→寫入，中間可能被插隊），
    只是鎖的資源從「一個時段」換成「一位主持人的整個行事曆」——沒有這把鎖，
    兩筆不同劇本／不同時段但共用同一位主持人的預約，可能同時通過衝突檢查、
    同時寫入，讓這位主持人被排進兩場重疊的場次。

    對不重複的 id 用排序過的順序逐一上鎖（sorted(set(...))）：多筆交易
    如果各自要鎖好幾位主持人，鎖的順序不一致就會有死鎖風險（A 先鎖 1 再等 2，
    B 先鎖 2 再等 1）；統一用同一個順序上鎖可以避免這個情況。
    """
    for host_id in sorted(set(host_ids)):
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"host|{host_id}",))


def _has_mmg_conflict(cur, mmg_id: int, session_date, session_time, start, end) -> bool:
    """這個劇本本身，在這段時間內是否已經有另一場（不同時段）真正成立
    （status='booked'）的場次跟它重疊——包含首尾相接（無縫接軌也算衝突）。

    刻意排除跟候選時段完全相同的 (session_date, session_time)：那些是
    同一場遊戲各序位的隊伍，是容量上限在管的事，不是這裡要擋的
    「劇本被排成兩場」。
    """
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM booking_record b
            JOIN mmg m ON m.id = b.mmg_id
            WHERE b.mmg_id = %s AND b.status = 'booked'
              AND NOT (b.session_date = %s AND b.session_time = %s)
              AND (b.session_date + b.session_time)::timestamp <= %s
              AND %s <= (b.session_date + b.session_time)::timestamp
                        + (m.period * interval '1 hour')
        )
        """,
        (mmg_id, session_date, session_time, end, start),
    )
    return cur.fetchone()[0]


def conflicting_host(
    cur, host_ids: list[int], mmg_id: int, session_date, session_time, start, end
) -> int | None:
    """host_ids 裡任何一位在這段時間有沒有別的（不同場次的）已成立預約，
    回傳第一個撞期的 user id；都沒撞回傳 None。

    排除條件是「跟候選場次完全相同的 (mmg_id, session_date, session_time)」，
    不是「這位主持人」本身——同一場遊戲底下各序位掛的是同一位主持人，
    那是同一件事被記錄很多次，不是這位主持人被排了兩場不同的戲。
    """
    for host_id in host_ids:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM booking_record b
                JOIN mmg m ON m.id = b.mmg_id
                WHERE b.status = 'booked'
                  AND %s IN (b.gm1_user_id, b.gm2_user_id, b.gm3_user_id, b.gm4_user_id)
                  AND NOT (b.mmg_id = %s AND b.session_date = %s AND b.session_time = %s)
                  AND (b.session_date + b.session_time)::timestamp <= %s
                  AND %s <= (b.session_date + b.session_time)::timestamp
                            + (m.period * interval '1 hour')
            )
            """,
            (host_id, mmg_id, session_date, session_time, end, start),
        )
        if cur.fetchone()[0]:
            return host_id
    return None


def find_conflicts(cur, booking_id: int) -> list[dict]:
    """這筆預約指派到的主持人，在這個時段是不是已經有別的已成立場次。

    問的事情跟 conflicting_host() 一樣，差別在回傳什麼：那一支只說「撞到
    了、是哪位主持人」，因為它的用途是擋下寫入；這一支要餵給對話框，所以
    連撞到的是哪一場、幾點都得帶回去——畫面只說「有衝突」而講不出衝突在
    哪裡，等於要人自己去翻。

    只看 status = 'booked'。同一個時段有好幾組在排隊是正常的（那正是序位
    的意義），真正的衝突是「已經成立的那一場把這位主持人佔住了」。

    這是提醒用的查詢，不是把關：主持人按確認、管理員切狀態，都不會因為
    撞期就被系統擋下來（那些畫面存在的目的就是人工判斷）。真正會擋的是
    create_booking()，那裡有 advisory lock。
    """
    cur.execute(
        """
        SELECT b.mmg_id, b.session_date, b.session_time, m.period,
               b.gm1_user_id, b.gm2_user_id, b.gm3_user_id, b.gm4_user_id
          FROM booking_record b JOIN mmg m ON m.id = b.mmg_id
         WHERE b.id = %s
        """,
        (booking_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise BookingError(f"預約 id={booking_id} 不存在")

    mmg_id, session_date, session_time, period = row[0], row[1], row[2], row[3]
    host_ids = [h for h in row[4:8] if h is not None]
    if not host_ids or period is None:
        return []

    start = datetime.combine(session_date, session_time)
    end = start + timedelta(hours=float(period))

    cur.execute(
        """
        SELECT u.id, u.name, m2.name, b2.session_date, b2.session_time
          FROM booking_record b2
          JOIN mmg m2  ON m2.id = b2.mmg_id
          JOIN users u ON u.id = ANY(%(host_ids)s)
                      AND u.id IN (b2.gm1_user_id, b2.gm2_user_id,
                                   b2.gm3_user_id, b2.gm4_user_id)
         WHERE b2.status = 'booked'
           AND b2.id <> %(booking_id)s
           -- 排除「同一場遊戲的其他序位」：那是同一件事被記錄很多次，
           -- 不是這位主持人被排了兩場不同的戲。跟 conflicting_host()
           -- 用同一條排除條件。
           AND NOT (b2.mmg_id = %(mmg_id)s
                    AND b2.session_date = %(d)s
                    AND b2.session_time = %(t)s)
           AND (b2.session_date + b2.session_time)::timestamp <= %(end)s
           AND %(start)s <= (b2.session_date + b2.session_time)::timestamp
                            + (m2.period * interval '1 hour')
         ORDER BY b2.session_date, b2.session_time
        """,
        {
            "host_ids": host_ids, "booking_id": booking_id, "mmg_id": mmg_id,
            "d": session_date, "t": session_time, "start": start, "end": end,
        },
    )
    return [
        {
            "host_id": r[0],
            "host_name": r[1],
            "mmg_name": r[2],
            "session_date": r[3].isoformat(),
            "session_time": r[4].strftime("%H:%M"),
        }
        for r in cur.fetchall()
    ]


def _validate_gm_choice(cur, mmg_id: int, gm_user_ids: list[int | None]) -> None:
    """確認玩家挑的主持人真的會帶這個角色。

    這條規則資料庫擋不了：booking_record.gm1_user_id 的外鍵只保證「是一個
    存在的使用者」，不保證「這個人在 mmg.gm1_user 的名單裡」。跨表的成員
    檢查要寫成約束得用 trigger，不划算，所以在這裡擋。
    """
    cur.execute(
        "SELECT name, gm1_name, gm2_name, gm3_name, gm4_name,"
        "       gm1_user, gm2_user, gm3_user, gm4_user "
        "FROM mmg WHERE id = %s",
        (mmg_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise BookingError(f"劇本 id={mmg_id} 不存在")

    mms_name, *rest = row
    gm_names, gm_pools = rest[0:4], rest[4:8]

    for i, (chosen, name, pool_) in enumerate(zip(gm_user_ids, gm_names, gm_pools), start=1):
        if name is None:
            if chosen is not None:
                raise BookingError(f"《{mms_name}》沒有角色 {i}，不能指定主持人")
            continue
        if chosen is None:
            raise BookingError(f"《{mms_name}》的角色 {i}「{name}」尚未指定主持人")
        if chosen not in (pool_ or []):
            raise BookingError(
                f"使用者 id={chosen} 不在《{mms_name}》角色 {i}「{name}」的可選名單中"
            )


def create_booking(
    cur,
    *,
    mmg_id: int,
    player_id: int,
    session_date,
    session_time,
    gm_user_ids: list[int | None],
    request_id: str | None = None,
    deposit: int = 0,
    note: str | None = None,
) -> BookingResult:
    """建立一筆預約，回傳它的順位。

    gm_user_ids 是四個位置的主持人 user id，沒有的角色放 None。

    request_id 是冪等鍵，應該由前端在玩家「按下預約」當下產生，該次意圖的
    所有重試都沿用同一組。這裡允許不給（自動產生一組），只是為了讓腳本與
    測試方便；**正式的預約路徑一定要由呼叫端傳入**，自動產生等於每次重試
    都是新的意圖，冪等保護會失效。
    """
    request_id = request_id or str(uuid.uuid4())
    host_ids = [i for i in gm_user_ids if i is not None]

    _lock_slot(cur, mmg_id, session_date, session_time)
    _lock_hosts(cur, host_ids)
    _validate_gm_choice(cur, mmg_id, gm_user_ids)

    # 容量 = 1 + waitlist_limit 組（欄位名沿用，指的是第 2 序位之後的上限）；
    # period 用來換算這場實際
    # 佔用到幾點，供劇本／主持人的排程衝突檢查用。
    cur.execute("SELECT name, waitlist_limit, period FROM mmg WHERE id = %s", (mmg_id,))
    mms_name, waitlist_limit, period = cur.fetchone()
    capacity = 1 + waitlist_limit

    start = datetime.combine(session_date, session_time)
    end = start + timedelta(hours=float(period))

    if _has_mmg_conflict(cur, mmg_id, session_date, session_time, start, end):
        raise BookingError(f"《{mms_name}》這段時間已經排了另一場，時段衝突")

    conflicting = conflicting_host(cur, host_ids, mmg_id, session_date, session_time, start, end)
    if conflicting is not None:
        cur.execute("SELECT name FROM users WHERE id = %s", (conflicting,))
        host_name = cur.fetchone()[0]
        raise BookingError(f"{host_name}在這個時間有場次無法預定")

    cur.execute(
        "SELECT COUNT(*) FROM booking_record "
        "WHERE mmg_id = %s AND session_date = %s AND session_time = %s "
        "  AND status <> 'cancelled'",
        (mmg_id, session_date, session_time),
    )
    current = cur.fetchone()[0]

    cur.execute(
        """
        INSERT INTO booking_record
            (mmg_id, player_id, session_date, session_time, deposit, note,
             gm1_user_id, gm2_user_id, gm3_user_id, gm4_user_id, request_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (request_id) DO NOTHING
        RETURNING id
        """,
        (mmg_id, player_id, session_date, session_time, deposit, note, *gm_user_ids, request_id),
    )
    row = cur.fetchone()

    if row is None:
        # 這個 request_id 先前就寫進去了——同一次意圖的重試。
        # 回報成功並附上原本那筆，不是失敗：玩家只按了一次，
        # 中間是網路出問題，不該讓他以為預約沒成立而再按一次。
        cur.execute("SELECT id FROM booking_record WHERE request_id = %s", (request_id,))
        booking_id = cur.fetchone()[0]
        already_existed = True
    else:
        # 容量檢查放在 INSERT 之後才判斷，是為了讓重試能先被上面攔下來：
        # 一個已經成立的預約即使現在時段滿了，重試時仍該回報它原本的狀態，
        # 而不是因為「滿了」被拒絕。
        if current >= capacity:
            raise BookingError(
                f"《{mms_name}》{session_date} {session_time} 已額滿"
                f"（最多 {1 + waitlist_limit} 組）"
            )
        booking_id = row[0]
        already_existed = False

    position = get_position(cur, booking_id)
    return BookingResult(id=booking_id, position=position, already_existed=already_existed)


def get_position(cur, booking_id: int) -> int:
    """算出某筆預約在它那個時段的序位。1 是最先預定的那一組。

    順位不存欄位、每次算——取消時只要把自己的 status 改掉，後面的人
    就自動遞補，沒有第二個地方需要同步（理由見 schema.py）。
    """
    cur.execute(
        """
        SELECT position FROM (
            SELECT id, ROW_NUMBER() OVER (
                       PARTITION BY mmg_id, session_date, session_time
                       ORDER BY created_at
                   ) AS position
            FROM booking_record
            WHERE status <> 'cancelled'
              AND (mmg_id, session_date, session_time) = (
                  SELECT mmg_id, session_date, session_time
                  FROM booking_record WHERE id = %s
              )
        ) ranked WHERE id = %s
        """,
        (booking_id, booking_id),
    )
    row = cur.fetchone()
    if row is None:
        raise BookingError(f"預約 id={booking_id} 已取消或不存在")
    return row[0]


def list_slot(cur, mmg_id: int, session_date, session_time) -> list[tuple]:
    """列出某時段的所有有效預約，依順位排序。"""
    cur.execute(
        """
        SELECT ROW_NUMBER() OVER (ORDER BY b.created_at) AS position,
               b.id, u.name, b.status, b.deposit, b.created_at, b.request_id
        FROM booking_record b
        JOIN users u ON u.id = b.player_id
        WHERE b.mmg_id = %s AND b.session_date = %s AND b.session_time = %s
          AND b.status <> 'cancelled'
        ORDER BY b.created_at
        """,
        (mmg_id, session_date, session_time),
    )
    return cur.fetchall()


# 允許的狀態值。與 schema.py 的說明一致：
#   gm_confirm   等待主持人確認
#   gm_reviewed  主持人已審閱
#   booked       已成立
#   cancelled    已取消
VALID_STATUSES = ("gm_confirm", "gm_reviewed", "booked", "ended", "cancelled")


def get_booking(cur, booking_id: int) -> dict | None:
    """單筆預約的完整資料，含玩家與劇本名稱。"""
    cur.execute(
        """
        SELECT b.id, b.mmg_id, m.name, b.player_id, u.name, u.email,
               b.session_date, b.session_time, b.status, b.deposit,
               m.booking_cost, b.note, b.created_at,
               b.gm1_user_id, b.gm2_user_id, b.gm3_user_id, b.gm4_user_id
          FROM booking_record b
          JOIN mmg m   ON m.id = b.mmg_id
          JOIN users u ON u.id = b.player_id
         WHERE b.id = %s
        """,
        (booking_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "mmg_id": row[1],
        "mmg_name": row[2],
        "player_id": row[3],
        "player_name": row[4],
        "player_email": row[5],
        "session_date": row[6].isoformat(),
        "session_time": row[7].strftime("%H:%M"),
        "status": row[8],
        "deposit": row[9],
        "booking_cost": row[10],
        "note": row[11],
        "created_at": row[12].isoformat(),
        "gm_user_ids": list(row[13:17]),
    }


def list_player_bookings(cur, player_id: int, *, include_cancelled: bool = False) -> list[dict]:
    """某位玩家的預約，新的在前。順位即時算出來，不從欄位讀。"""
    cur.execute(
        """
        SELECT b.id, m.name, b.session_date, b.session_time, b.status,
               b.deposit, m.booking_cost, b.note, b.created_at,
               CASE WHEN b.status = 'cancelled' THEN NULL ELSE (
                   SELECT COUNT(*) + 1 FROM booking_record x
                    WHERE x.mmg_id = b.mmg_id
                      AND x.session_date = b.session_date
                      AND x.session_time = b.session_time
                      AND x.status <> 'cancelled'
                      AND x.created_at < b.created_at
               ) END AS position
          FROM booking_record b
          JOIN mmg m ON m.id = b.mmg_id
         WHERE b.player_id = %s
           AND (%s OR b.status <> 'cancelled')
         ORDER BY b.session_date DESC, b.session_time DESC, b.created_at DESC
        """,
        (player_id, include_cancelled),
    )
    out = []
    for r in cur.fetchall():
        out.append({
            "id": r[0], "mmg_name": r[1],
            "session_date": r[2].isoformat(), "session_time": r[3].strftime("%H:%M"),
            "status": r[4], "deposit": r[5], "booking_cost": r[6],
            "note": r[7], "created_at": r[8].isoformat(),
            "position": r[9],
            "is_first": r[9] == 1 if r[9] is not None else False,
        })
    return out


def cancel_booking(cur, booking_id: int, *, note: str | None = None) -> dict:
    """取消一筆預約。

    只改自己這一列的 status，**不做任何遞補處理**——順位是每次由
    created_at 算出來的，後面的人自動往前遞補。這正是不存 priority 欄位
    換來的好處：這裡沒有「把下一位升上來」的第二次寫入可以失敗。
    """
    cur.execute(
        """
        UPDATE booking_record
           SET status = 'cancelled',
               note = COALESCE(%s, note)
         WHERE id = %s AND status <> 'cancelled'
        RETURNING id
        """,
        (note, booking_id),
    )
    if cur.fetchone() is None:
        cur.execute("SELECT 1 FROM booking_record WHERE id = %s", (booking_id,))
        if cur.fetchone() is None:
            raise BookingError(f"預約 id={booking_id} 不存在")
        raise BookingError("這筆預約已經取消過了")
    return get_booking(cur, booking_id)


def record_deposit(cur, booking_id: int, amount: int) -> dict:
    """記錄實際收到的訂金金額。

    通知玩家繳訂金、確認款項是否已到帳，是管理員在系統外做的事；這裡
    只負責把金額寫進去。狀態要不要跟著跳到 booked，在同一句 UPDATE 裡
    讓資料庫判斷，不由呼叫端另外決定：

        目前 status = 'gm_reviewed'，且新金額 >= 該劇本的 booking_cost
        → 這句 UPDATE 一併把 status 轉成 'booked'

    只在 gm_reviewed 這個狀態才會自動轉——主持人都還沒審過的場次，
    就算訂金收滿了也不該直接跳過審核變成成立。若 mmg.booking_cost
    是 NULL（劇本沒設訂金金額），比較結果恆為 NULL、CASE 不會命中，
    狀態就停在原地，需要用 set_status() 另外手動處理。

    用 >=（達到即可）而不是嚴格等於：多收一點不該卡住轉態，也允許
    分兩次記錄金額湊足（先記一部分、之後補到滿額）。如果你要的是
    金額必須跟劇本要求分毫不差才轉態，這裡改成 = 即可。
    """
    cur.execute(
        """
        UPDATE booking_record b
           SET deposit = %s,
               status = CASE
                          WHEN b.status = 'gm_reviewed'
                               AND %s >= (SELECT booking_cost FROM mmg WHERE id = b.mmg_id)
                          THEN 'booked'
                          ELSE b.status
                        END
         WHERE b.id = %s AND b.status <> 'cancelled'
        RETURNING id
        """,
        (amount, amount, booking_id),
    )
    if cur.fetchone() is None:
        cur.execute("SELECT 1 FROM booking_record WHERE id = %s", (booking_id,))
        if cur.fetchone() is None:
            raise BookingError(f"預約 id={booking_id} 不存在")
        raise BookingError("已取消的預約不能記錄訂金")
    return get_booking(cur, booking_id)


def confirm_host(cur, booking_id: int, host_id: int) -> dict:
    """某位主持人確認自己在這場的指定。

    只有 status 還是 gm_confirm 時才能確認——已經 gm_reviewed 以後代表
    全部人都確認過了，沒有「重新確認」這個動作；已取消／已結束更不用說。

    「確認」針對的是這個人，不是這一格：萬一同一位主持人剛好同時佔了
    這場的兩個角色（前端字串比對會擋，但後端不假設前端一定擋得住），
    一次確認會把他佔的每一格都標記，不需要對同一個人分別按兩次。

    確認完之後，若全部被指定的角色都確認過了，這句 UPDATE 一併把整筆
    status 從 gm_confirm 轉成 gm_reviewed，不留給呼叫端另外判斷——
    跟 record_deposit() 的轉態方式是同一個原則：轉態條件在寫入的那一刻
    由資料庫自己算。
    """
    # COALESCE(...,false) 每一個都不能省：gm{n}_user_id 是 NULL 時
    # (gm{n}_user_id = %(host)s) 在 SQL 三值邏輯下算出來是 NULL，
    # 不是 false，而 `false OR NULL` 還是 NULL——寫進 NOT NULL 欄位
    # 直接違反約束（實測撞過：NotNullViolation on gm4_confirmed）。
    cur.execute(
        """
        UPDATE booking_record
           SET gm1_confirmed = gm1_confirmed OR COALESCE(gm1_user_id = %(host)s, false),
               gm2_confirmed = gm2_confirmed OR COALESCE(gm2_user_id = %(host)s, false),
               gm3_confirmed = gm3_confirmed OR COALESCE(gm3_user_id = %(host)s, false),
               gm4_confirmed = gm4_confirmed OR COALESCE(gm4_user_id = %(host)s, false)
         WHERE id = %(id)s
           AND status = 'gm_confirm'
           AND %(host)s IN (gm1_user_id, gm2_user_id, gm3_user_id, gm4_user_id)
           -- 這個人還沒確認過。少了這行，重複確認會靜靜地成功
           -- （OR 是冪等的，true OR true 還是 true），呼叫端拿到 200
           -- 卻什麼都沒發生——UI 上那一列早就變暗了，回一個假的成功
           -- 比回錯誤更難查。
           AND NOT (
                 COALESCE(gm1_user_id = %(host)s AND gm1_confirmed, false)
              OR COALESCE(gm2_user_id = %(host)s AND gm2_confirmed, false)
              OR COALESCE(gm3_user_id = %(host)s AND gm3_confirmed, false)
              OR COALESCE(gm4_user_id = %(host)s AND gm4_confirmed, false)
           )
        RETURNING
            (gm1_user_id IS NULL OR gm1_confirmed)
        AND (gm2_user_id IS NULL OR gm2_confirmed)
        AND (gm3_user_id IS NULL OR gm3_confirmed)
        AND (gm4_user_id IS NULL OR gm4_confirmed)
        """,
        {"host": host_id, "id": booking_id},
    )
    row = cur.fetchone()
    if row is None:
        # 沒更新到任何列。分辨是哪一種情況，回一個說得清楚的錯誤——
        # 三種都回同一句話的話，主持人看到「你不是指定的主持人」
        # 卻明明看得到這場，會不知道發生什麼事。
        cur.execute(
            """
            SELECT status,
                   COALESCE(%(host)s IN (gm1_user_id, gm2_user_id,
                                         gm3_user_id, gm4_user_id), false),
                   COALESCE(gm1_user_id = %(host)s AND gm1_confirmed, false)
                OR COALESCE(gm2_user_id = %(host)s AND gm2_confirmed, false)
                OR COALESCE(gm3_user_id = %(host)s AND gm3_confirmed, false)
                OR COALESCE(gm4_user_id = %(host)s AND gm4_confirmed, false)
              FROM booking_record WHERE id = %(id)s
            """,
            {"host": host_id, "id": booking_id},
        )
        existing = cur.fetchone()
        if existing is None:
            raise BookingError(f"預約 id={booking_id} 不存在")
        status, is_host, already = existing
        if not is_host:
            raise BookingError("你不是這場預約指定的主持人")
        if already:
            raise BookingError("你已經確認過這場預約了")
        raise BookingError("這筆預約已經不在待確認狀態")

    if row[0]:
        cur.execute(
            "UPDATE booking_record SET status = 'gm_reviewed' "
            "WHERE id = %s AND status = 'gm_confirm'",
            (booking_id,),
        )
    return get_booking(cur, booking_id)


STATUS_LABEL = {
    "gm_confirm": "等待其他主持確認",
    "gm_reviewed": "等待訂金支付",
    "booked": "場次已確認",
    "ended": "已結束",
    "cancelled": "已取消",
}


_HOST_ROLE_NAME = """
    CASE WHEN b.gm1_user_id = %(host)s THEN m.gm1_name
         WHEN b.gm2_user_id = %(host)s THEN m.gm2_name
         WHEN b.gm3_user_id = %(host)s THEN m.gm3_name
         WHEN b.gm4_user_id = %(host)s THEN m.gm4_name
    END
"""

_HOST_CONFIRMED = """
    CASE WHEN b.gm1_user_id = %(host)s THEN b.gm1_confirmed
         WHEN b.gm2_user_id = %(host)s THEN b.gm2_confirmed
         WHEN b.gm3_user_id = %(host)s THEN b.gm3_confirmed
         WHEN b.gm4_user_id = %(host)s THEN b.gm4_confirmed
    END
"""

PAGE_SIZE = 50

# 「狀態」篩選器給前端的選項。刻意只有這三個：
# 待確認頁籤永遠只有 gm_confirm、已結束頁籤永遠只有 ended，
# 對那兩個頁籤篩狀態等於沒篩，所以這個篩選器只出現在已確認頁籤，
# 而已確認頁籤只可能出現這三種狀態。
CONFIRMED_TAB_STATUSES = ("gm_confirm", "gm_reviewed", "booked")


def filter_clauses(f: dict | None) -> tuple[str, dict]:
    """把篩選條件轉成 SQL 片段與參數。空值一律視為「不篩」。

    字串比對用 ILIKE '%值%'（包含、不分大小寫）。實測過這不會是效能
    瓶頸：字串比對只作用在 mmg（劇本）與 users（會員）這兩張小表上，
    筆數取決於店裡有幾齣劇本、幾個會員，不隨場次紀錄增長。兩萬筆
    booking_record 加上兩個 ILIKE 實測 17ms，其中 ILIKE 的部分因為
    對象只有個位數列、且被 Memoize 快取，成本可以忽略。

    值裡的 % 和 _ 會先跳脫掉：不跳脫的話，使用者輸入 "%" 會變成
    「符合任何東西」，輸入 "_" 會變成「任何單一字元」——那不是篩選器
    該有的行為，使用者打進去的字元應該就是字面意思。
    """
    if not f:
        return "", {}

    def like(v: str) -> str:
        return "%" + v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    sql, params = [], {}
    if f.get("mmg_name"):
        sql.append("AND m.name ILIKE %(f_mmg)s")
        params["f_mmg"] = like(f["mmg_name"])
    if f.get("player_name"):
        sql.append("AND u.name ILIKE %(f_player)s")
        params["f_player"] = like(f["player_name"])
    if f.get("role_name"):
        # 只比對「我擔任的那個角色」的名稱，不是這場所有角色——
        # 這一欄在畫面上顯示的就是我自己的角色，篩選要跟顯示一致。
        sql.append(f"AND {_HOST_ROLE_NAME} ILIKE %(f_role)s")
        params["f_role"] = like(f["role_name"])
    if f.get("session_date"):
        sql.append("AND b.session_date = %(f_date)s")
        params["f_date"] = f["session_date"]
    if f.get("session_time"):
        sql.append("AND b.session_time = %(f_time)s")
        params["f_time"] = f["session_time"]
    if f.get("status"):
        sql.append("AND b.status = %(f_status)s")
        params["f_status"] = f["status"]
    return "\n           ".join(sql), params


def paged(cur, sql: str, params: dict, row_to_item) -> dict:
    """撈一頁，多要一筆判斷還有沒有下一頁，回傳前把那多要的一筆丟掉。

    不用 COUNT(*) 另外查總數——分頁箭頭只需要知道「有沒有下一頁」，
    不需要知道「總共幾頁」，COUNT 一次全表在場次這種會不斷長大
    的表上是白花的成本。
    """
    cur.execute(sql, {**params, "limit": PAGE_SIZE + 1})
    rows = cur.fetchall()
    has_more = len(rows) > PAGE_SIZE
    return {"items": [row_to_item(r) for r in rows[:PAGE_SIZE]], "has_more": has_more}


# 三個頁籤各自的 WHERE 條件與排序。差異只在這裡，其餘查詢結構共用。
#
# 待確認 vs 已確認的分野，是「這個人自己的 gm{n}_confirmed」，不是整筆
# 的 status——一齣戲兩位主持人，我確認了、對方還沒確認時，整筆 status
# 仍是 gm_confirm，但這場對「我」而言已經確認過了，該出現在已確認
# （顯示「等待其他主持確認」），不是待確認（那裡不該再讓我按一次）。
_TABS = {
    "pending": {
        "where": f"AND b.status = 'gm_confirm' AND NOT COALESCE({_HOST_CONFIRMED}, false)",
        "order": "b.session_date, b.session_time",
    },
    "confirmed": {
        "where": f"""AND (
                 b.status IN ('gm_reviewed', 'booked')
                 OR (b.status = 'gm_confirm' AND {_HOST_CONFIRMED})
               )""",
        "order": "b.session_date, b.session_time",
    },
    "ended": {
        "where": "AND b.status = 'ended'",
        "order": "b.session_date DESC, b.session_time DESC",
    },
}


def host_tab(
    cur,
    host_id: int,
    tab: str,
    *,
    start: int = 1,
    filters: dict | None = None,
) -> dict:
    """撈主持人介面「某一個頁籤」的一頁資料。

    分頁與篩選都是當前頁籤的事，不影響另外兩個頁籤——所以它們是這支
    函式的參數，而不是 host_dashboard() 的。使用者翻頁或按下篩選時，
    前端只針對當前頁籤呼叫這支，另外兩包維持畫面上既有的內容。

    start 是 1-based 的起始筆數（1、51、101...）。換篩選條件時前端要把
    start 重設回 1——舊的頁碼在新條件下可能根本不存在。

    filters 全部欄位都可省略（省略＝不篩）：
        mmg_name / player_name / role_name  字串，包含比對、不分大小寫
        session_date                        date，精確等於
        session_time                        time，精確等於
        status                              只對 confirmed 頁籤有意義；
                                            另兩個頁籤各自只有一種狀態，
                                            傳了也不會有作用（見下）
    """
    if tab not in _TABS:
        raise BookingError(f"未知的頁籤「{tab}」")

    # 待確認永遠只有 gm_confirm、已結束永遠只有 ended，對它們篩狀態
    # 等於沒篩或直接清空；狀態篩選器在畫面上也只出現在已確認頁籤。
    effective = dict(filters or {})
    if tab != "confirmed":
        effective.pop("status", None)

    where, fp = filter_clauses(effective)

    def item(r):
        return {
            "id": r[0], "mmg_name": r[1], "player_name": r[2],
            "session_date": r[3].isoformat(), "session_time": r[4].strftime("%H:%M"),
            "status": r[5], "status_label": STATUS_LABEL[r[5]], "role_name": r[6],
        }

    spec = _TABS[tab]
    return paged(
        cur,
        f"""
        SELECT b.id, m.name, u.name, b.session_date, b.session_time, b.status,
               {_HOST_ROLE_NAME} AS role_name
          FROM booking_record b
          JOIN mmg m   ON m.id = b.mmg_id
          JOIN users u ON u.id = b.player_id
         WHERE %(host)s IN (b.gm1_user_id, b.gm2_user_id, b.gm3_user_id, b.gm4_user_id)
           {spec["where"]}
           {where}
         ORDER BY {spec["order"]}
         LIMIT %(limit)s OFFSET %(offset)s
        """,
        {"host": host_id, "offset": start - 1, **fp},
        item,
    )


def host_dashboard(cur, host_id: int) -> dict:
    """主持人介面初次載入：三個頁籤各拿第一頁，一支 API 打完。

    只給「剛打開頁面」用，刻意不收分頁與篩選參數——那兩件事都只作用於
    當前頁籤，走 host_tab() 單獨拿即可，不需要為了改動一個頁籤而把
    另外兩個也重查一次。
    """
    return {tab: host_tab(cur, host_id, tab) for tab in _TABS}


def mark_ended(cur, booking_id: int) -> dict:
    """人工把一筆已成立的預約標記為結束。

    只能從 booked 轉過來——沒收訂金、沒經過主持人審核的場次談不上
    「結束」，那種情況要嘛還卡在審核流程，要嘛該走 cancel_booking。
    """
    cur.execute(
        "UPDATE booking_record SET status = 'ended' "
        "WHERE id = %s AND status = 'booked' RETURNING id",
        (booking_id,),
    )
    if cur.fetchone() is None:
        cur.execute("SELECT status FROM booking_record WHERE id = %s", (booking_id,))
        row = cur.fetchone()
        if row is None:
            raise BookingError(f"預約 id={booking_id} 不存在")
        raise BookingError(f"只有已成立（booked）的預約能標記結束，目前狀態是「{row[0]}」")
    return get_booking(cur, booking_id)


def mark_ended_batch(cur) -> int:
    """把所有「已成立、且場次時間加上劇本時長已經過去」的預約批次轉成
    ended。設計成排程呼叫（建議每天 00:00 一次），不是每次查詢時即時
    判斷——理由見 schema.py：status 欄位裡存的值才是唯一事實，不該讓
    同一筆資料的狀態隨「現在幾點」在讀取當下臨時算出不同答案。

    ★ 時區地雷：session_date/session_time 是不帶時區的「牆上時鐘」
    （台北時間的字面值，約定成俗，見 schema.py 的說明），NOW() 回傳的
    是有時區的當下瞬間。兩者不能直接比大小——(date+time)::timestamp
    是一個裸值，拿去跟 NOW() 比較時，Postgres 會用「連線當下的 session
    TimeZone 設定」去解讀它，如果那個設定剛好不是 Asia/Taipei（例如
    連線預設是 UTC），比較結果會整整偏移 8 小時，讓場次提早或延後
    被標記結束，而且不會報錯，只會默默算錯。用 `AT TIME ZONE
    'Asia/Taipei'` 明確告訴 Postgres 這個裸值是台北時間，轉成正確的
    時間點之後再跟 NOW() 比較，結果就不受連線的 session 設定影響。
    """
    cur.execute(
        """
        UPDATE booking_record b
           SET status = 'ended'
          FROM mmg m
         WHERE b.mmg_id = m.id
           AND b.status = 'booked'
           AND (b.session_date + b.session_time) AT TIME ZONE 'Asia/Taipei'
                 + (m.period * interval '1 hour') <= NOW()
        """
    )
    return cur.rowcount


def set_status(cur, booking_id: int, status: str, *, note: str | None = None) -> dict:
    """更新狀態。取消請走 cancel_booking，不要用這支。"""
    if status not in VALID_STATUSES:
        raise BookingError(f"未知的狀態「{status}」")
    if status == "cancelled":
        raise BookingError("取消請用 cancel_booking，它會一併處理備註")

    cur.execute(
        """
        UPDATE booking_record SET status = %s, note = COALESCE(%s, note)
         WHERE id = %s AND status <> 'cancelled'
        RETURNING id
        """,
        (status, note, booking_id),
    )
    if cur.fetchone() is None:
        cur.execute("SELECT status FROM booking_record WHERE id = %s", (booking_id,))
        row = cur.fetchone()
        if row is None:
            raise BookingError(f"預約 id={booking_id} 不存在")
        raise BookingError("已取消的預約不能再改狀態")
    return get_booking(cur, booking_id)
