"""排期：某個劇本在某個日期、某個時間開不開放預約。

## 四層，由高到低

    0. 上下架區間（mmg.start_booking / end_booking）
       絕對外框。區間外一律不開，下面三層都翻不過來。
       start_booking 是「時刻」不是「日期」：開放當天只有開放預約時間
       之後的場次算數，同一天更早的時段仍然不開。

    1. 特定日期例外（mmg_schedule_exception）
       區間內最高。「這一週跟平常不同」——臨時公休、臨時加場、
       當天時段跟平常不一樣。

    2. 每週常態（mmg_weekly_slot）
       沒被例外蓋到的日子走這裡。「每週都這樣」——平日／週末的差異
       屬於這一層。

    3. 兜底
       第 2 層一列都沒有 ＝ 全部關閉，只靠例外日一天一天開。

第 3 層是這個模型能保持簡單的原因：五種情境（只開週末／只開平日／
只開指定日／平常開臨時休／平常不開臨時加場）共用同一套結構，
沒有 if-else 分岔，也不需要任何「模式」旗標。

## ★ 這支是唯一的判斷來源

前端日曆、slot 訊息、以及 create_booking 的最後防線，三個地方都必須
呼叫這裡，不可以各自實作一份。前端反白的日子跟後端擋下來的日子只要
有一點不一致，使用者就會看到「明明可以選卻訂不下去」——而那種 bug
從畫面上完全看不出是哪一層判斷不同。
"""

from __future__ import annotations

from datetime import date, time


def open_times(cur, mmg_id: int, session_date: date) -> list[time] | None:
    """這一天開放的時段清單。不開放（任何一層擋下）時回傳 None。

    ★ 沒有「還沒設定所以不限制」這種例外。start_booking 是 NOT NULL，
      每一齣劇本一定有上架日；沒有排定任何星期或特例的劇本就是全部關閉。
      一齣連開放時段都沒設的戲，本來就不該被預定——這是刻意的，不是漏網。

    ★ None 與 [] 不同：[] 不會發生（兩張表都有 CHECK 擋掉空時段）。
      呼叫端用 `is None` 判斷，不要用真假值——空清單跟 None 在 if 裡
      是一樣的，那會讓兩種不同的狀態悄悄合併。
    """
    # 第 0 層。這支在開賣尖峰是最熱的讀取路徑之一，能少一次往返就少一次
    # 讓 Neon compute 保持清醒的理由（見 db/idle_gap.py）。
    #
    # 「現在」也在同一句裡取，不另外呼叫 listing.now_taipei()：理由跟它一樣
    # 要用資料庫的時鐘（判定與寫入各用一個時間來源會自相矛盾），但熱路徑上
    # 不值得為它多付一次往返。
    cur.execute(
        "SELECT start_booking, end_booking, (NOW() AT TIME ZONE 'Asia/Taipei') "
        "FROM mmg WHERE id = %s",
        (mmg_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    start, end, now = row
    if session_date < start.date():
        return None
    if end is not None and session_date > end.date():
        return None

    # 第 1 層：這一天有沒有例外。有就以它為準，完全不看常態。
    #
    # ★ 例外日也要經過 _bookable。原本這裡直接 return list(times)，沒有套
    #   上下架邊界——而 calendar() 那邊有套，兩處因此不一致：上架日當天剛好
    #   是例外日時，日曆會濾掉上架時刻之前的場次，這裡卻放行（2026-09-12
    #   修正，順帶讓「已經過去」的過濾對例外日也生效）。
    cur.execute(
        "SELECT is_open, times FROM mmg_schedule_exception "
        "WHERE mmg_id = %s AND exception_date = %s",
        (mmg_id, session_date),
    )
    row = cur.fetchone()
    if row is not None:
        is_open, times = row
        return _bookable(list(times) if is_open else None, session_date, start, end, now)

    # 第 2 層：每週常態。沒有那一列就是那個星期不開（第 3 層兜底）。
    # weekday 用 0=週日 … 6=週六，與 JavaScript 的 Date.getDay() 一致，
    # 前端不用換算。Python 的 date.weekday() 是 0=週一，所以要轉。
    cur.execute(
        "SELECT times FROM mmg_weekly_slot WHERE mmg_id = %s AND weekday = %s",
        (mmg_id, session_date.isoweekday() % 7),
    )
    row = cur.fetchone()
    times = list(row[0]) if row is not None else None
    return _bookable(times, session_date, start, end, now)


def _bookable(times, session_date: date, start, end, now) -> list[time] | None:
    """把一天的時段裡「實際訂得到的」留下來：上下架邊界，以及已經過去的。

    open_times() 與 calendar() 都走這一支，而且兩條路（例外日、每週常態）
    都要走——四層之中任何一層的時段，最後都得經過同一套邊界。這一支如果
    被繞過，前端日曆跟 create_booking 的防線就會各說各話。

    ## 上下架邊界

    ★ 這一步不能省。前面只比到「日期」，所以 14:00 上架的戲在當天會連
      10:00 的場次一起放行——那正是只存日期表達不出來、而案主要求要能
      表達的那件事。上架日與下架日之間的每一天都不受影響。

    邊界方向刻意不同：上架時刻「當下」就開賣（>=），下架時刻「當下」
    就停賣（<）。兩邊都用 >= 的話，剛好等於下架時刻的那一場會落在
    已經下架的區間裡還能訂。

    ## 已經過去的（2026-09-12 加）

    原本四層都只看「日期」，沒有任何一層比「現在」——只要上架日在過去，
    連過去的日期都訂得下去。那一筆會直接進排程的清理範圍，eli 那種直接
    成立的組態下甚至會被轉成「已結束」，在歷史裡留下一場沒發生過的遊戲。

    今天只留還沒開始的場次（t > now，嚴格大於：剛好此刻開場的那一場也不給訂，
    按下送出的當下它就已經開始了）。更早的日期整天都不開。

    ★ 管理員補登過去的場次不受這條影響——那條路走 allow_closed_slot，
      根本不問排期（見 create_booking）。
    """
    if times is None:
        return times
    if session_date < now.date():
        return None
    if session_date == start.date():
        times = [t for t in times if t >= start.time()]
    if end is not None and session_date == end.date():
        times = [t for t in times if t < end.time()]
    if session_date == now.date():
        times = [t for t in times if t > now.time()]
    # 全部被濾掉時回 None 而不是 []：呼叫端用 `is None` 判斷「這天不開」，
    # 留一個空清單會讓「開了但沒有時段」混進來，那不是一個真實的狀態。
    return times or None


def is_open(cur, mmg_id: int, session_date: date, session_time: time) -> bool:
    """這個 (日期, 時間) 能不能預約。"""
    times = open_times(cur, mmg_id, session_date)
    if times is None:
        return False
    # 只比時與分。資料庫存的是 TIME，前端送的是 HH:MM，兩邊都不帶秒；
    # 但若哪天有人手動塞了帶秒的資料，直接比物件會無聲地永遠不相等。
    return any(t.hour == session_time.hour and t.minute == session_time.minute
               for t in times)


def calendar(cur, mmg_id: int, start: date, end: date) -> tuple[dict, bool]:
    """一段期間內每個開放日期對應的時段，給前端日曆用。

    回傳 (每天的時段, 有沒有設定過)。「有沒有設定過」跟著一起回，是為了
    讓呼叫端一次往返就拿到兩件事——它跟日期資料來自同一批查詢。

    只回傳「有開」的日期；沒出現在結果裡的日期就是不開。讓前端用
    「有沒有這個 key」判斷，而不是回一堆 is_open=false 的空殼——
    一個月裡不開的日子通常比開的多。

    ★ 三個查詢就做完，不是一天一次。一個月 31 天逐日呼叫 open_times
      是 93 次往返；同區 Neon 一次約 4ms，看起來還好，但這支是使用者
      每翻一個月就打一次的端點，而每一次往返都在讓 compute 保持清醒
      （見 db/idle_gap.py 的成本說明）。
    """
    # 「現在」併在同一句裡取，理由同 open_times()：資料庫的時鐘、零額外往返。
    cur.execute(
        "SELECT start_booking, end_booking, (NOW() AT TIME ZONE 'Asia/Taipei') "
        "FROM mmg WHERE id = %s",
        (mmg_id,),
    )
    row = cur.fetchone()
    if row is None:
        return {}, False
    lo, hi, now = row

    cur.execute(
        "SELECT weekday, times FROM mmg_weekly_slot WHERE mmg_id = %s",
        (mmg_id,),
    )
    weekly = {r[0]: list(r[1]) for r in cur.fetchall()}

    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM mmg_schedule_exception WHERE mmg_id = %s)",
        (mmg_id,),
    )
    # 「排得出場」＝有星期常態或特例日。上架日期不算，因為那是必填的，
    # 每一齣都有；拿它當判準的話這個旗標永遠是真，就沒有意義了。
    configured = bool(weekly or cur.fetchone()[0])

    # 第 0 層直接把查詢範圍夾掉，後面兩層就不必再各自判斷一次。
    # 只夾到「日」——上架當天要保留下來，讓 _bookable 去濾掉那天
    # 上架時刻之前的時段；整天砍掉的話上架當天就完全訂不到了。
    if lo.date() > start:
        start = lo.date()
    # 同樣只夾到「日」，下架當天要留下來讓 _bookable 濾掉
    # 下架時刻之後的時段。
    if hi is not None and hi.date() < end:
        end = hi.date()
    # 過去的日子整天都不開，直接從今天起算——省下逐日跑一遍再全部丟掉。
    # 今天本身要留，讓 _bookable 濾掉今天已經開始的場次。
    if now.date() > start:
        start = now.date()
    if start > end:
        return {}, configured

    cur.execute(
        "SELECT exception_date, is_open, times FROM mmg_schedule_exception "
        "WHERE mmg_id = %s AND exception_date BETWEEN %s AND %s",
        (mmg_id, start, end),
    )
    exceptions = {r[0]: (r[1], list(r[2])) for r in cur.fetchall()}

    out: dict[str, list[str]] = {}
    day = start
    while day <= end:
        if day in exceptions:
            is_open_day, times = exceptions[day]
            times = times if is_open_day else None
        else:
            times = weekly.get(day.isoweekday() % 7)
        times = _bookable(times, day, lo, hi, now)
        if times:
            out[day.isoformat()] = [t.strftime("%H:%M") for t in sorted(times)]
        day = day.fromordinal(day.toordinal() + 1)
    return out, configured


# ── 管理員的寫入 ────────────────────────────────────────────


def get_rules(cur, mmg_id: int) -> dict:
    """管理員介面用：這個劇本的完整排期設定。"""
    cur.execute(
        "SELECT weekday, times FROM mmg_weekly_slot WHERE mmg_id = %s ORDER BY weekday",
        (mmg_id,),
    )
    weekly = [
        {"weekday": r[0], "times": [t.strftime("%H:%M") for t in sorted(r[1])]}
        for r in cur.fetchall()
    ]
    cur.execute(
        "SELECT exception_date, is_open, times FROM mmg_schedule_exception "
        "WHERE mmg_id = %s ORDER BY exception_date",
        (mmg_id,),
    )
    exceptions = [
        {
            "date": r[0].isoformat(),
            "is_open": r[1],
            "times": [t.strftime("%H:%M") for t in sorted(r[2])],
        }
        for r in cur.fetchall()
    ]
    return {"weekly": weekly, "exceptions": exceptions}


def list_all_rules(cur) -> dict[int, dict]:
    """所有劇本的排期設定，給管理員的劇本清單一次載入。

    兩個查詢就好，不是每齣戲各查一次——劇本清單是管理員每次切到那個
    頁籤都會打的，N+1 在這裡會直接變成幾十次往返。

    ★ 這裡刻意撈「全部」的特例日期，包含已經過去的。理由是 set_rules
      是整批覆寫：如果讀取端只給未來的日期，儲存時就會把過去的那些
      一起刪掉——畫面上從來沒出現過的資料被無聲刪除。
      哪天為了效能要在這裡加日期篩選，set_rules 的 DELETE 一定要加上
      同一個條件，兩邊必須是同一個範圍。
    """
    cur.execute("SELECT mmg_id, weekday, times FROM mmg_weekly_slot ORDER BY mmg_id, weekday")
    out: dict[int, dict] = {}
    for mid, wd, times in cur.fetchall():
        out.setdefault(mid, {"weekly": [], "exceptions": []})["weekly"].append(
            {"weekday": wd, "times": [t.strftime("%H:%M") for t in sorted(times)]}
        )
    cur.execute(
        "SELECT mmg_id, exception_date, is_open, times FROM mmg_schedule_exception "
        "ORDER BY mmg_id, exception_date"
    )
    for mid, day, open_day, times in cur.fetchall():
        out.setdefault(mid, {"weekly": [], "exceptions": []})["exceptions"].append(
            {
                "date": day.isoformat(),
                "is_open": open_day,
                "times": [t.strftime("%H:%M") for t in sorted(times)],
            }
        )
    return out


class ScheduleError(Exception):
    """排期設定不合法。訊息是給管理員看的。"""


def _clean_times(raw, where: str) -> list[str]:
    """把時段清單正規化成 HH:MM，並擋掉空的與格式錯的。"""
    if not isinstance(raw, list) or not raw:
        raise ScheduleError(f"{where}：至少要有一個時段")
    out = []
    for item in raw:
        text = str(item).strip()
        parts = text.split(":")
        if len(parts) < 2:
            raise ScheduleError(f"{where}：時段格式錯誤「{text}」")
        try:
            h, m = int(parts[0]), int(parts[1])
        except ValueError:
            raise ScheduleError(f"{where}：時段格式錯誤「{text}」") from None
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ScheduleError(f"{where}：時段超出範圍「{text}」")
        value = f"{h:02d}:{m:02d}"
        if value not in out:
            out.append(value)
    return sorted(out)


def set_rules(cur, mmg_id: int, weekly: list, exceptions: list) -> None:
    """整批覆寫排期設定。

    ★ 刻意是「整批換掉」而不是逐筆增刪：管理員在畫面上看到的就是完整的
      一份設定，送回來的也是完整的一份。逐筆增刪要前端記住哪幾筆被刪了、
      哪幾筆改了，那份帳一旦跟後端對不起來，就會留下畫面上看不到、
      卻真的在擋人訂位的殘留規則。
    """
    cleaned_weekly = []
    for item in weekly or []:
        try:
            wd = int(item.get("weekday"))
        except (TypeError, ValueError):
            raise ScheduleError("星期格式錯誤") from None
        if not 0 <= wd <= 6:
            raise ScheduleError(f"星期超出範圍：{wd}")
        cleaned_weekly.append((wd, _clean_times(item.get("times"), f"星期 {wd}")))
    if len({wd for wd, _ in cleaned_weekly}) != len(cleaned_weekly):
        raise ScheduleError("同一個星期不能設定兩次")

    cleaned_exceptions = []
    for item in exceptions or []:
        day = str(item.get("date") or "").strip()
        if not day:
            raise ScheduleError("特例日期不能空白")
        open_day = bool(item.get("is_open"))
        # 關閉的那一天不看時段——「整天不開」沒有時段可言。前端就算送了
        # 一串過來也直接丟掉，免得畫面上留著一組永遠不會生效的時間。
        times = _clean_times(item.get("times"), f"{day}") if open_day else []
        cleaned_exceptions.append((day, open_day, times))
    if len({d for d, _, _ in cleaned_exceptions}) != len(cleaned_exceptions):
        raise ScheduleError("同一個日期不能設定兩筆特例")

    cur.execute("DELETE FROM mmg_weekly_slot WHERE mmg_id = %s", (mmg_id,))
    for wd, times in cleaned_weekly:
        cur.execute(
            "INSERT INTO mmg_weekly_slot (mmg_id, weekday, times) VALUES (%s, %s, %s)",
            (mmg_id, wd, times),
        )
    cur.execute("DELETE FROM mmg_schedule_exception WHERE mmg_id = %s", (mmg_id,))
    for day, open_day, times in cleaned_exceptions:
        cur.execute(
            "INSERT INTO mmg_schedule_exception "
            "(mmg_id, exception_date, is_open, times) VALUES (%s, %s, %s, %s)",
            (mmg_id, day, open_day, times),
        )
