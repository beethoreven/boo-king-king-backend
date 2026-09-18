"""忙碌日：店家的與主持人的。

## 兩種忙碌日，擋的東西不同

    store  整家店那天不開。**玩家一律訂不到**，比排期的任何一層都高——
           上下架、特定日期例外、每週常態全部翻不過來。
           但**管理員仍然排得進去**：場次管理那條路本來就不問排期（補登
           已經發生的場、電話預約的特殊場），忙碌日跟著同一條規矩，只是
           要先經過一個對話框（案主 2026-09-18）。

    gm     那位主持人那天不能被指定。**硬擋，管理員也不能放行**
           （案主 2026-09-18 更正）：它跟「主持人撞期」同一級，差別只是
           撞期是系統算出來的、忙碌日是他自己標的。要排就得請他先解除。

## 為什麼不是設定，也不是排期的一部分

排期（mmg_weekly_slot／mmg_schedule_exception）是綁在單一劇本上的，
表達不了「整家店那天不開」或「這個人那天不行」。所以是自己一張表。

## 為什麼 store 那幾列的 user_id 是空的

忙碌日是「整家店」的，不屬於哪一位管理員——任何管理員設的都一樣。
CHECK 把「來源」與「是誰」綁在一起，不會出現標成 gm 卻沒有人、或標成
store 卻掛了人。
"""

from __future__ import annotations

from datetime import date

STORE = "store"
GM = "gm"


def store_busy(cur, session_date) -> bool:
    """這一天是不是店家忙碌日。

    ★ 這支會被排期判斷呼叫，而排期是開賣尖峰最熱的讀取路徑之一。查的是
      一張最多幾百列的小表、而且走唯一索引，成本可以忽略；但**不要改成
      逐日呼叫**——要一整個月的話用 dates_in()，一次查完。
    """
    cur.execute(
        "SELECT 1 FROM busy_day WHERE busy_source = %s AND busy_date = %s",
        (STORE, session_date),
    )
    return cur.fetchone() is not None


def busy_gm_ids(cur, gm_ids, session_date) -> list[int]:
    """這些主持人裡面，哪幾位把這一天標成忙碌。沒有就回空清單。"""
    ids = [i for i in (gm_ids or []) if i]
    if not ids:
        return []
    cur.execute(
        "SELECT user_id FROM busy_day "
        " WHERE busy_source = %s AND busy_date = %s AND user_id = ANY(%s)",
        (GM, session_date, ids),
    )
    return [r[0] for r in cur.fetchall()]


def dates_in(cur, source: str, user_id: int | None, year: int, month: int) -> list[str]:
    """某個月的忙碌日（ISO 字串），給行事曆把格子塗灰用。

    半開區間 [這個月 1 號, 下個月 1 號)：閏年與大小月不必自己判斷。
    """
    first = date(year, month, 1)
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    cur.execute(
        "SELECT busy_date FROM busy_day "
        " WHERE busy_source = %(source)s"
        "   AND user_id IS NOT DISTINCT FROM %(user)s"
        "   AND busy_date >= %(first)s AND busy_date < %(next)s"
        " ORDER BY busy_date",
        {"source": source, "user": user_id, "first": first, "next": nxt},
    )
    return [r[0].isoformat() for r in cur.fetchall()]


def add(cur, source: str, user_id: int | None, busy_date) -> bool:
    """設成忙碌日。回傳「這次真的新增了嗎」——本來就是忙碌日就回 False。

    ON CONFLICT DO NOTHING 搭配兩條部分唯一索引：連點兩下、或網路重送，
    都只會有一列。回傳值用來決定要不要寄信——已經是忙碌日的再按一次，
    不該再寄一輪通知。
    """
    cur.execute(
        "INSERT INTO busy_day (busy_source, user_id, busy_date) VALUES (%s, %s, %s) "
        "ON CONFLICT DO NOTHING RETURNING id",
        (source, user_id, busy_date),
    )
    return cur.fetchone() is not None


def remove(cur, source: str, user_id: int | None, busy_date) -> bool:
    """取消忙碌日。回傳「本來有沒有這一列」。"""
    cur.execute(
        "DELETE FROM busy_day "
        " WHERE busy_source = %(source)s"
        "   AND user_id IS NOT DISTINCT FROM %(user)s"
        "   AND busy_date = %(date)s",
        {"source": source, "user": user_id, "date": busy_date},
    )
    return cur.rowcount > 0


def sessions_on(cur, busy_date, *, gm_id: int | None = None) -> list[dict]:
    """那一天既存的場次（**非取消**），設忙碌日時要提醒與寄信用。

    gm_id 有給就只算指定到那位主持人的——他標自己忙碌時，要看的是自己
    的場次，不是全店的。

    ★ 已結束的場次也算（案主 2026-09-18）：管理員跑去動一個已經結束的
      日期，本身就是值得被看見的操作。
    """
    where = "b.status <> 'cancelled' AND b.session_date = %(date)s"
    params: dict = {"date": busy_date}
    if gm_id is not None:
        where += (" AND %(gm)s IN (b.gm1_user_id, b.gm2_user_id,"
                  " b.gm3_user_id, b.gm4_user_id)")
        params["gm"] = gm_id
    cur.execute(
        f"""
        SELECT b.id, m.name, b.session_time, b.status
          FROM booking_record b
          JOIN mmg m ON m.id = b.mmg_id
         WHERE {where}
         ORDER BY b.session_time, b.created_at
        """,
        params,
    )
    return [
        {"id": r[0], "mmg_name": r[1], "session_time": r[2].strftime("%H:%M"),
         "status": r[3]}
        for r in cur.fetchall()
    ]
