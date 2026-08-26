"""
管理員介面「場次管理」的資料存取層。

子頁籤按 status 分（跟主持人介面按「我自己確認了沒」分不同——那是
站在某位主持人的視角，這裡是全站視角）。因為頁籤本身就是狀態，
所以列表不顯示 status、篩選器也沒有 status 這一項；要改狀態是在
編輯單筆時做。

分頁與篩選都只作用於當前子頁籤，沿用主持人介面定下來的原則。
"""

from __future__ import annotations

from db.bookings import (
    PAGE_SIZE,
    VALID_STATUSES,
    BookingError,
    filter_clauses,
    paged,
    record_deposit,
)

# 子頁籤 = status，順序即畫面上的順序（跟著流程走）
TABS = {
    "gm_confirm": "待主持確認",
    "gm_reviewed": "待收訂金",
    "booked": "已成立",
    "ended": "已結束",
    "cancelled": "已取消",
}

# 已結束與已取消看最近的，其餘看最早的——待處理的事情要先做舊的，
# 已經完結的則是最近的比較常查。
_DESC_TABS = ("ended", "cancelled")


def list_tab(cur, tab: str, *, start: int = 1, filters: dict | None = None) -> dict:
    if tab not in TABS:
        raise BookingError(f"未知的頁籤「{tab}」")

    # status 由頁籤決定，篩選器不該再有一個——傳了就忽略，避免出現
    # 「在待收訂金頁籤篩 booked」這種必然空集合的組合。
    effective = {k: v for k, v in (filters or {}).items() if k != "status"}
    where, fp = filter_clauses(effective)
    order = (
        "b.session_date DESC, b.session_time DESC"
        if tab in _DESC_TABS
        else "b.session_date, b.session_time"
    )

    def item(r):
        return {
            "id": r[0], "mmg_id": r[1], "mmg_name": r[2],
            "player_id": r[3], "player_name": r[4],
            "session_date": r[5].isoformat(), "session_time": r[6].strftime("%H:%M"),
            "deposit": r[7], "booking_cost": r[8], "note": r[9],
            "gm_user_ids": list(r[10:14]),
            "gm_confirmed": list(r[14:18]),
            "gm_names": list(r[18:22]),
        }

    return paged(
        cur,
        f"""
        SELECT b.id, b.mmg_id, m.name, b.player_id, u.name,
               b.session_date, b.session_time, b.deposit, m.booking_cost, b.note,
               b.gm1_user_id, b.gm2_user_id, b.gm3_user_id, b.gm4_user_id,
               b.gm1_confirmed, b.gm2_confirmed, b.gm3_confirmed, b.gm4_confirmed,
               m.gm1_name, m.gm2_name, m.gm3_name, m.gm4_name
          FROM booking_record b
          JOIN mmg m   ON m.id = b.mmg_id
          JOIN users u ON u.id = b.player_id
         WHERE b.status = %(tab)s
           {where}
         ORDER BY {order}
         LIMIT %(limit)s OFFSET %(offset)s
        """,
        {"tab": tab, "offset": start - 1, **fp},
        item,
    )


def overview(cur) -> dict:
    """初次載入：五個子頁籤各第一頁。"""
    return {tab: list_tab(cur, tab) for tab in TABS}


def update(cur, booking_id: int, data: dict) -> bool:
    """編輯一筆場次。回傳是否真的改到任何欄位。

    可編輯：劇本、預定者、日期、時間、狀態、訂金、備註，以及各角色的
    主持人與「是否已確認」。

    訂金沿用 record_deposit() 的自動轉態（gm_reviewed + 金額足額 →
    booked），但**手動指定的狀態優先**：管理員在這個畫面同時改了狀態和
    訂金時，以他選的狀態為準。理由是這個介面存在的目的就是人工介入，
    自動規則不該覆蓋掉管理員的明確意圖。
    """
    cur.execute(
        """
        SELECT mmg_id, player_id, session_date, session_time, status, deposit, note,
               gm1_user_id, gm2_user_id, gm3_user_id, gm4_user_id,
               gm1_confirmed, gm2_confirmed, gm3_confirmed, gm4_confirmed
          FROM booking_record WHERE id = %s
        """,
        (booking_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise BookingError(f"預約 id={booking_id} 不存在")

    current = {
        "mmg_id": row[0], "player_id": row[1],
        "session_date": row[2], "session_time": row[3],
        "status": row[4], "deposit": row[5], "note": row[6],
        "gm_user_ids": list(row[7:11]), "gm_confirmed": list(row[11:15]),
    }

    status = data.get("status")
    if status not in VALID_STATUSES:
        raise BookingError(f"未知的狀態「{status}」")

    gm_user_ids = data.get("gm_user_ids") or [None] * 4
    gm_confirmed = data.get("gm_confirmed") or [False] * 4
    if len(gm_user_ids) != 4 or len(gm_confirmed) != 4:
        raise BookingError("gm_user_ids 與 gm_confirmed 都需要剛好 4 個元素")
    # 沒有指派主持人的角色不可能「已確認」——放著會讓
    # confirm_host() 的「全員確認了嗎」判斷得到錯誤結果。
    gm_confirmed = [bool(c) and u is not None for c, u in zip(gm_confirmed, gm_user_ids)]

    new = {
        "mmg_id": data.get("mmg_id"), "player_id": data.get("player_id"),
        "session_date": data.get("session_date"), "session_time": data.get("session_time"),
        "status": status, "deposit": int(data.get("deposit") or 0),
        "note": (data.get("note") or "").strip() or None,
        "gm_user_ids": list(gm_user_ids), "gm_confirmed": gm_confirmed,
    }
    if all(new[k] == current[k] for k in current):
        return False

    cur.execute(
        """
        UPDATE booking_record SET
            mmg_id = %s, player_id = %s, session_date = %s, session_time = %s,
            status = %s, deposit = %s, note = %s,
            gm1_user_id = %s, gm1_confirmed = %s,
            gm2_user_id = %s, gm2_confirmed = %s,
            gm3_user_id = %s, gm3_confirmed = %s,
            gm4_user_id = %s, gm4_confirmed = %s
         WHERE id = %s
        """,
        (
            new["mmg_id"], new["player_id"], new["session_date"], new["session_time"],
            new["status"], new["deposit"], new["note"],
            new["gm_user_ids"][0], new["gm_confirmed"][0],
            new["gm_user_ids"][1], new["gm_confirmed"][1],
            new["gm_user_ids"][2], new["gm_confirmed"][2],
            new["gm_user_ids"][3], new["gm_confirmed"][3],
            booking_id,
        ),
    )

    # 訂金的自動轉態只在「管理員沒有自己動狀態」時才跑。動了的話
    # 以他選的為準（見 docstring）。
    if new["status"] == current["status"] and new["deposit"] != current["deposit"]:
        record_deposit(cur, booking_id, new["deposit"])

    return True


def deposit_warning(deposit: int, booking_cost: int | None) -> str | None:
    """訂金金額相對於劇本定價的警示文字，給管理員介面顯示用。

    純顯示，不影響任何寫入判斷——record_deposit() 用 >= 判斷是否轉態，
    超收一樣算足額。
    """
    if booking_cost is None or deposit == 0:
        return None
    if deposit > booking_cost:
        return "超收訂金"
    if deposit < booking_cost:
        return "訂金不足額"
    return None
