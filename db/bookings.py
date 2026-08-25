"""
建立與查詢預約。

這裡實作的是 schema.py 的 _ensure_booking_record docstring 記載的那套流程：
advisory lock → 檢查容量 → 冪等寫入 → 算順位。不要繞過這支直接對
booking_record 下 INSERT，那會跳過容量檢查與冪等保護。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


class BookingError(Exception):
    """預約無法成立。訊息是給使用者看的。"""


@dataclass
class BookingResult:
    id: int
    position: int          # 1 = 正選，2 以上 = 候補
    already_existed: bool  # True 代表這個 request_id 先前就成功過

    @property
    def is_confirmed(self) -> bool:
        return self.position == 1

    @property
    def label(self) -> str:
        return "正選" if self.is_confirmed else f"候補 {self.position - 1}"


def _lock_slot(cur, mms_id: int, session_date, session_time) -> None:
    """鎖住這個時段，直到交易結束。

    為什麼需要：容量檢查要擋的是「第 N+1 筆」，而那筆在檢查當下還不存在，
    沒有既有的列可以鎖（phantom read）。只有先序列化同一時段的預約，
    「先數再寫」才是安全的。

    用 advisory lock 而不是鎖 mms 那一列：鎖 mms 會把該劇本所有日期、
    所有時段的預約全部序列化，開賣尖峰時會把不相干的時段也卡住。
    advisory lock 的鍵是「劇本+日期+時間」，只擋真正在搶同一場的人。

    xact 版本在交易結束時自動釋放，包含 rollback——不需要、也不應該
    手動解鎖，否則某條例外路徑忘了解就會卡死整個時段。

    hashtext 理論上可能碰撞，讓兩個不同時段共用同一把鎖。那只會讓它們
    多序列化一點，不會造成錯誤結果，在這個規模可以接受。
    """
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        (f"booking|{mms_id}|{session_date}|{session_time}",),
    )


def _validate_gm_choice(cur, mms_id: int, gm_user_ids: list[int | None]) -> None:
    """確認玩家挑的主持人真的會帶這個角色。

    這條規則資料庫擋不了：booking_record.gm1_user_id 的外鍵只保證「是一個
    存在的使用者」，不保證「這個人在 mms.gm1_user 的名單裡」。跨表的成員
    檢查要寫成約束得用 trigger，不划算，所以在這裡擋。
    """
    cur.execute(
        "SELECT name, gm1_name, gm2_name, gm3_name, gm4_name,"
        "       gm1_user, gm2_user, gm3_user, gm4_user "
        "FROM mms WHERE id = %s",
        (mms_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise BookingError(f"劇本 id={mms_id} 不存在")

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
    mms_id: int,
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

    _lock_slot(cur, mms_id, session_date, session_time)
    _validate_gm_choice(cur, mms_id, gm_user_ids)

    # 容量 = 1 組正選 + waitlist_limit 組候補
    cur.execute("SELECT name, waitlist_limit FROM mms WHERE id = %s", (mms_id,))
    mms_name, waitlist_limit = cur.fetchone()
    capacity = 1 + waitlist_limit

    cur.execute(
        "SELECT COUNT(*) FROM booking_record "
        "WHERE mms_id = %s AND session_date = %s AND session_time = %s "
        "  AND status <> 'cancelled'",
        (mms_id, session_date, session_time),
    )
    current = cur.fetchone()[0]

    cur.execute(
        """
        INSERT INTO booking_record
            (mms_id, player_id, session_date, session_time, deposit, note,
             gm1_user_id, gm2_user_id, gm3_user_id, gm4_user_id, request_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (request_id) DO NOTHING
        RETURNING id
        """,
        (mms_id, player_id, session_date, session_time, deposit, note, *gm_user_ids, request_id),
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
                f"（1 組正選 + {waitlist_limit} 組候補）"
            )
        booking_id = row[0]
        already_existed = False

    position = get_position(cur, booking_id)
    return BookingResult(id=booking_id, position=position, already_existed=already_existed)


def get_position(cur, booking_id: int) -> int:
    """算出某筆預約在它那個時段的順位。1 = 正選。

    順位不存欄位、每次算——取消時只要把自己的 status 改掉，後面的人
    就自動遞補，沒有第二個地方需要同步（理由見 schema.py）。
    """
    cur.execute(
        """
        SELECT position FROM (
            SELECT id, ROW_NUMBER() OVER (
                       PARTITION BY mms_id, session_date, session_time
                       ORDER BY created_at
                   ) AS position
            FROM booking_record
            WHERE status <> 'cancelled'
              AND (mms_id, session_date, session_time) = (
                  SELECT mms_id, session_date, session_time
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


def list_slot(cur, mms_id: int, session_date, session_time) -> list[tuple]:
    """列出某時段的所有有效預約，依順位排序。"""
    cur.execute(
        """
        SELECT ROW_NUMBER() OVER (ORDER BY b.created_at) AS position,
               b.id, u.name, b.status, b.deposit, b.created_at, b.request_id
        FROM booking_record b
        JOIN users u ON u.id = b.player_id
        WHERE b.mms_id = %s AND b.session_date = %s AND b.session_time = %s
          AND b.status <> 'cancelled'
        ORDER BY b.created_at
        """,
        (mms_id, session_date, session_time),
    )
    return cur.fetchall()
