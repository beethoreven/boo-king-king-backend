"""使用者「現在」是什麼狀態。**讀 users.status 一律經過這裡。**

## 為什麼不能直接比 status = 'active'

停權有期限（users.banned_until），而到期那一刻不會有人把欄位改回 active——
那需要一個每分鐘醒來的排程，會讓 Neon 永遠不休眠（理由見 db/listing.py）。
所以欄位裡的字最多會落後事實七天，直到每週排程（guard.decay_points）順手補上。

    存的字   banned_until    當下生效的狀態
    banned   未來的時刻      banned
    banned   已經過去        active   ← 欄位說 banned，事實是 active
    banned   NULL            banned（無限期，要管理員手動解）
    其他     （不看）        欄位的字

直接比欄位的話，停權到期的人會「登得進來、用得了，但 LINE bot 不認他、
收不到推播、不在主持人名單裡」，而且沒有任何錯誤（2026-11-10 查到三處）。

tests/test_user_status.py 會掃整個後端，出現直接拿 users 的狀態字比對的
SQL 就失敗。
"""

from __future__ import annotations


def effective_status_sql(alias: str = "") -> str:
    """一段 SQL 運算式，算出當下生效的狀態（'active'／'deactive'／'banned'／'deleted'）。

    alias 是 users 在那句查詢裡的別名（例如 JOIN 時的 "u"），沒有別名就留空。
    """
    c = f"{alias}." if alias else ""
    return (
        f"(CASE WHEN {c}status = 'banned'"
        f" AND {c}banned_until IS NOT NULL"
        f" AND {c}banned_until <= NOW()"
        f" THEN 'active' ELSE {c}status END)"
    )


def active_sql(alias: str = "") -> str:
    """WHERE 用的條件：這個人現在算不算啟用。"""
    return f"({effective_status_sql(alias)} = 'active')"
