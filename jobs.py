"""場次的批次整理。

每天 00:00 由排程觸發一次（cron-job.org 打 POST /api/jobs/daily），
管理員也可以從介面上的「場次整理」手動跑一次。兩者做的事一樣，差別只在
時間範圍：排程版只往回看兩天，手動版處理所有今天以前的場次。

觸發方式是 HTTP，不是 process 內的計時器：Render 的免費方案會讓服務
休眠，睡著的行程裡沒有計時器會醒來。

## 重跑是安全的

每一支都是「把符合條件的列改成某個狀態」，改完就不再符合條件。同一天跑
第二次只會回報 0 筆。這是刻意的，因為排程服務的重試只保證「至少送達
一次」，不保證「剛好一次」；而管理員按幾次「場次整理」也不該有差別。

## 出錯了會怎樣

三支各自跑在自己的交易裡。一支炸了不會把其他已經做完的事回捲，下一次
重跑會把剩下的補完，不需要人工介入。

整體回 500 的話 cron-job.org 會依它自己的設定重試；因為重跑安全，重試
不會有副作用。這裡不自己實作重試迴圈：排程服務本來就有，在應用層再做
一層只會讓「到底跑了幾次」變得難以追蹤。
"""

from __future__ import annotations

from db import bookings as bookings_db


def run_batch(pool, *, lookback_days) -> dict:
    """跑完三件事，回報各做了幾筆。

    lookback_days 只影響「把已成立的場次轉成 ended」那一支：
      2    —— 每日排程用。遊戲可能跨一個夜但不會跨兩個，兩天足夠。
      None —— 手動整理用。不設窗口，把漏跑那幾天累積的一次補完。

    另外兩支本來就是「日期已經過去」「建立超過 N 天」，沒有窗口的概念。

    刻意分成三個交易而不是包在一起：它們處理的是不相干的三群資料，
    一支失敗沒有理由把另外兩支已經正確完成的工作一起丟掉。
    """
    result: dict = {}
    errors: list[str] = []

    steps = (
        ("ended", lambda cur: bookings_db.mark_ended_batch(cur, lookback_days=lookback_days)),
        ("cancelled", bookings_db.cancel_expired_batch),
        ("waitlist_expired", bookings_db.expire_stale_waitlist_batch),
    )
    for key, fn in steps:
        try:
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    result[key] = fn(cur)
                    conn.commit()
        except Exception as exc:  # noqa: BLE001 - 要讓其他支照跑，不能讓例外中斷
            result[key] = None
            errors.append(f"{key}: {exc}")

    # 漏網的場次數：已經結束、卻超出回看窗口而不會再被處理的。手動整理
    # （lookback_days=None）跑完之後這個數字必然是 0，因為那一版沒有窗口。
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


def run_daily(pool) -> dict:
    """每日排程：只處理最近兩天。"""
    return run_batch(pool, lookback_days=bookings_db.ENDED_LOOKBACK_DAYS)


def run_full(pool) -> dict:
    """管理員手動整理：處理所有今天以前的場次，不設窗口。

    存在的理由是排程可能漏跑。與其為了防漏再疊一層自動補償機制（那需要
    記錄「上次成功跑到哪」，又是一組要維護的狀態），不如給人一個按鈕：
    看到 stale_booked 不是 0 就按一下，一次補完。
    """
    return run_batch(pool, lookback_days=None)
