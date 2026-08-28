"""每日排程要做的事。

觸發方式是 HTTP（cron-job.org 每天 00:00 打 POST /api/jobs/daily），
不是 process 內的計時器：Render 的免費方案會讓服務休眠，睡著的行程裡
沒有計時器會醒來。同樣的理由，也不用 APScheduler 之類的內嵌排程。

## 重跑是安全的

每一支都是「把符合條件的列改成某個狀態」，改完就不再符合條件。同一天跑
第二次只會回報 0 筆，不會做出重複的事。這是刻意的設計，因為排程服務的
重試機制只保證「至少送達一次」，不保證「剛好一次」。

## 出錯了會怎樣

兩支各自跑在自己的交易裡。第一支炸了不會把第二支已經做完的事回捲，
第二支炸了第一支也已經落地——一次部分成功的執行，下一次重跑會把剩下的
補完，不需要人工介入。

整體回 500 的話 cron-job.org 會依它自己的設定重試；而因為重跑安全，
重試不會有副作用。這裡不自己實作重試迴圈：排程服務本來就有，在應用層
再做一層只會讓「到底跑了幾次」變得難以追蹤。
"""

from __future__ import annotations

from db import bookings as bookings_db


def run_daily(pool) -> dict:
    """跑完每日的兩件事，回報各做了幾筆。

    刻意分成兩個交易而不是包在一起：它們處理的是不相干的兩群資料
    （已成立的場次該結束、沒走完流程的預約該作廢），一支失敗沒有理由
    把另一支已經正確完成的工作一起丟掉。
    """
    result: dict = {}
    errors: list[str] = []

    for key, fn in (
        ("ended", bookings_db.mark_ended_batch),
        ("cancelled", bookings_db.cancel_expired_batch),
    ):
        try:
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    result[key] = fn(cur)
                    conn.commit()
        except Exception as exc:  # noqa: BLE001 - 要讓另一支照跑，不能讓例外中斷
            result[key] = None
            errors.append(f"{key}: {exc}")

    # 漏網的場次數。正常是 0；不是 0 代表排程漏跑過，那些場次已經超出
    # mark_ended_batch 的回看窗口，靠自己不會再被處理。
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                result["stale_booked"] = bookings_db.count_stale_booked(cur)
    except Exception as exc:  # noqa: BLE001
        result["stale_booked"] = None
        errors.append(f"stale_booked: {exc}")

    if errors:
        result["errors"] = errors
    return result
