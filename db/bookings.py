"""
建立與查詢預約。

這裡實作的是 schema.py 的 _ensure_booking_record docstring 記載的那套流程：
advisory lock → 檢查容量 → 冪等寫入 → 算順位。不要繞過這支直接對
booking_record 下 INSERT，那會跳過容量檢查與冪等保護。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from db import functions, listing, schedule, slot_cache


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


# ── 平日場／假日場 ──────────────────────────────────────────────
#
# 每一筆預約記著自己是平日場還是假日場（booking_record.day_type），那決定
# 它比對劇本的哪一個訂金。建立時依日期推，之後管理員可以在場次管理手動
# 改——國定假日、補班日只有店家自己知道，系統只認得週六日。

DAY_TYPES = ("weekday", "holiday")
DAY_TYPE_LABEL = {"weekday": "平日", "holiday": "假日"}

# 一筆預約「該收多少訂金」。用在查詢裡，需要 booking_record 別名 b、
# mmg 別名 m。
#
# ★ 一筆預約的應收訂金一律從這裡取，不要直接讀 m.booking_cost——那個欄位
#   現在只是「平日訂金」。直接讀的話假日場會比對到平日的金額，而兩個金額
#   剛好一樣的店家（步經徑遷移後就是這樣）永遠看不出來寫錯了。
DUE_SQL = (
    "CASE WHEN b.day_type = 'holiday' "
    "THEN m.booking_cost_holiday ELSE m.booking_cost END"
)

# 還會走到 record_deposit 那道比對的狀態。這兩個狀態的預約，應收訂金
# 一定要有值——見 check_deposit_coverage。
AWAITING_DEPOSIT = ("gm_confirm", "gm_reviewed")


def default_day_type(session_date) -> str:
    """依日期推場次類型：週六日是假日場，其餘是平日場。

    schema.py 回填既有資料、前端場次管理換日期時帶的預設值，都是這條規則。
    """
    return "holiday" if session_date.isoweekday() % 7 in (0, 6) else "weekday"


def require_deposit_set(cur, mmg_id: int, day_type: str, *, creating: bool) -> None:
    """這齣戲在這個場次類型有設定訂金；沒有就擋下。

    0 算有設定（這齣免訂金），空才是沒設定——空的話 record_deposit 拿
    NULL 去比，這筆預約會永遠停在待收訂金，而且不會有任何錯誤。

    creating 只影響訊息：建立時講「不能建立」；編輯時擋下的原因可能是
    改了劇本、改了類型、或把狀態改回待收訂金，講「不能建立」會看不懂。

    ★ 不收訂金的店（functions 表 deposit=off）整段不適用：那些店的預約
      不會經過「待收訂金」，訂金欄位在畫面上也不存在，要求他們填金額
      只會讓每一次存檔都被擋下來。
    """
    if not functions.deposit_required(cur):
        return
    cur.execute(
        "SELECT name, CASE WHEN %s = 'holiday' THEN booking_cost_holiday "
        "ELSE booking_cost END FROM mmg WHERE id = %s",
        (day_type, mmg_id),
    )
    row = cur.fetchone()
    if row is None:
        raise BookingError(f"劇本 id={mmg_id} 不存在")
    name, due = row
    if due is None:
        label = DAY_TYPE_LABEL[day_type]
        if creating:
            raise BookingError(f"《{name}》還沒有設定{label}訂金，不能建立{label}場")
        raise BookingError(
            f"《{name}》還沒有設定{label}訂金，這筆{label}場之後收訂金時會沒有金額可以比對。"
            f"請先到劇本管理補上（不收訂金請填 0）"
        )


def check_deposit_coverage(cur, mmg_id: int) -> None:
    """劇本存檔的最後一道：有哪一種日子會產生預約，就必須有那一種訂金。

    「會產生預約」指三件事，任何一件成立就算：
      - 每週固定開放的星期（週六日 → 假日，其餘 → 平日）
      - 今天以後的特例開放日（依那天是星期幾）
      - 還在等訂金的既有預約（依它自己的場次類型——可能被手動改過）

    ★ 第三件不能省。一齣戲把週末關掉、再把假日訂金清空，前兩件都不成立，
      但先前訂下的週六場還在等訂金；讓它存下去，那幾筆之後收訂金時就沒有
      金額可比。

    跑在「劇本欄位與排期都寫完之後」、同一個交易裡：價格與排期是同一次
    存檔送出來的，只看其中一邊，「同時打開週末＋填上假日訂金」這種合法
    的改法會被舊值誤擋。

    ★ 不收訂金的店整段跳過，理由同 require_deposit_set。
    """
    if not functions.deposit_required(cur):
        return
    cur.execute(
        "SELECT name, booking_cost, booking_cost_holiday, "
        "(NOW() AT TIME ZONE 'Asia/Taipei')::date FROM mmg WHERE id = %s",
        (mmg_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise BookingError(f"劇本 id={mmg_id} 不存在")
    name, cost, cost_holiday, today = row
    missing = [t for t, v in (("weekday", cost), ("holiday", cost_holiday)) if v is None]
    if not missing:
        return

    cur.execute("SELECT weekday FROM mmg_weekly_slot WHERE mmg_id = %s", (mmg_id,))
    weekly = {r[0] for r in cur.fetchall()}
    cur.execute(
        "SELECT exception_date FROM mmg_schedule_exception "
        "WHERE mmg_id = %s AND is_open AND exception_date >= %s ORDER BY exception_date",
        (mmg_id, today),
    )
    open_days = [r[0] for r in cur.fetchall()]
    cur.execute(
        "SELECT day_type, COUNT(*) FROM booking_record "
        "WHERE mmg_id = %s AND status = ANY(%s) GROUP BY day_type",
        (mmg_id, list(AWAITING_DEPOSIT)),
    )
    pending = dict(cur.fetchall())

    problems = []
    for day_type in missing:
        label = DAY_TYPE_LABEL[day_type]
        holiday = day_type == "holiday"
        reasons = []
        # 週一排到週日，週六日排在最後——講「週六、週日」比「週日、週六」順。
        weekdays = sorted((w for w in weekly if (w in (0, 6)) == holiday),
                          key=lambda w: (w + 6) % 7)
        if weekdays:
            reasons.append("每週" + "、".join(f"週{'日一二三四五六'[w]}" for w in weekdays)
                           + "有開放時段")
        dates = [d for d in open_days if default_day_type(d) == day_type]
        if dates:
            shown = "、".join(f"{d.month}/{d.day}" for d in dates[:3])
            reasons.append(f"{shown}{'等日' if len(dates) > 3 else ''}有特別開放")
        if pending.get(day_type):
            reasons.append(f"還有 {pending[day_type]} 筆{label}場在等待收訂金")
        if reasons:
            problems.append(f"{'，'.join(reasons)}，必須設定{label}訂金")
    if problems:
        raise BookingError(f"《{name}》" + "；".join(problems) + "（不收訂金請填 0）")


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


def _lock_gms(cur, gm_ids: list[int]) -> None:
    """鎖住這幾位主持人的行事曆，直到交易結束。

    跟 _lock_slot 是同一個 TOCTOU 問題（查沒衝突→寫入，中間可能被插隊），
    只是鎖的資源從「一個時段」換成「一位主持人的整個行事曆」——沒有這把鎖，
    兩筆不同劇本／不同時段但共用同一位主持人的預約，可能同時通過衝突檢查、
    同時寫入，讓這位主持人被排進兩場重疊的場次。

    對不重複的 id 用排序過的順序逐一上鎖（sorted(set(...))）：多筆交易
    如果各自要鎖好幾位主持人，鎖的順序不一致就會有死鎖風險（A 先鎖 1 再等 2，
    B 先鎖 2 再等 1）；統一用同一個順序上鎖可以避免這個情況。

    ★ 鎖的字串（"gm|<id>"）是**跨行程的協定值**，不是單純的命名。兩個行程
      要用同一個字串才會拿到同一把鎖，所以改這個字串的那一次部署，新舊行程
      交替的那幾秒鐘會各鎖各的，這段時間這把鎖等於不存在。這裡的量是一天
      個位數，挑沒人在訂的時間部署就好；但下次要再改這個字串時要記得這件事。
    """
    for gm_id in sorted(set(gm_ids)):
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"gm|{gm_id}",))


# 一場戲實際佔用包廂的區間。
#
#   [開始 - ready_time_cost, 開始 + period + reset_time_cost]
#
# 佈置與還原期間包廂一樣不能給別人用，所以佔用的不只是 period。兩場戲的
# 佔用區間有重疊就是衝突。
#
# ★ 這個寫法讓「往前」與「往後」自動都對，不必分開判斷。展開來看，X 較早、
#   Y 較晚時，重疊的條件會化簡成
#       Y.開始 < X.開始 + X.period + X.reset + Y.ready
#   ——正是「前一場做完、收完，再加上我要的佈置時間」。反過來由 X 看出去
#   是同一條不等式，所以兩個方向不會有不一致的答案。
#
# ★ 用 < 而不是 <=：緩衝時間現在由 ready/reset 明確表達了，端點剛好相接
#   代表「收拾完、也佈置完，下一場正好開始」，那是允許的。改成 <= 等於把
#   同一段緩衝算兩次。（在有這兩個欄位之前，規則是 <=，靠它來擠出換場
#   時間；那個理由已經不存在。）
_OCCUPY_START = "(b.session_date + b.session_time)::timestamp - (m2.ready_time_cost * interval '1 hour')"
_OCCUPY_END = ("(b.session_date + b.session_time)::timestamp "
               "+ ((m2.period + m2.reset_time_cost) * interval '1 hour')")


def has_room_conflict(cur, mmg_id: int, session_date, session_time) -> tuple | None:
    """這齣戲要用的那間包廂，在這段時間內是不是已經被別的成立場次佔著。
    撞到的話回傳 (劇本名, 日期, 時間, period, reset)，沒撞回傳 None。

    ★ 比對的對象是「包廂」不是「劇本」。同一間包廂裡，10:00 開演的 12
      小時本做到 22:00、收到 22:30，20:00 就不能再開另一齣戲——即使那是
      完全不同的劇本。

    佔用區間包含佈置與還原時間，兩邊各用自己的（見上方 _OCCUPY_*）。
    候選場次的區間直接在 SQL 裡從 mmg 讀，不由呼叫端算好傳進來：時長、
    佈置、還原三個值都在資料庫，繞出去一趟只是多一個會不同步的地方。

    排除「同一個劇本的同一個時段」：那是同一場遊戲的各個序位，不是被排了
    兩場。不同劇本在同一個時段就是真的撞了，不能排除。
    """
    # 這家店不卡包廂就沒有這件事。eli 那種一人一室、同一時間只可能有一場的
    # 店，包廂卡場等於重複擋一次主持人卡場已經擋掉的東西（functions 表的
    # conflict_room）。
    if not functions.room_conflict_on(cur):
        return None

    cur.execute(
        f"""
        WITH me AS (
            SELECT room_id,
                   (%(d)s + %(t)s)::timestamp - (ready_time_cost * interval '1 hour') AS occupy_start,
                   (%(d)s + %(t)s)::timestamp
                     + ((period + reset_time_cost) * interval '1 hour') AS occupy_end
              FROM mmg WHERE id = %(mmg)s
        )
        SELECT m2.name, b.session_date, b.session_time, m2.period, m2.reset_time_cost
          FROM booking_record b
          JOIN mmg m2 ON m2.id = b.mmg_id
          CROSS JOIN me
         WHERE b.status = 'booked'
           AND m2.room_id = me.room_id
           AND NOT (b.mmg_id = %(mmg)s
                    AND b.session_date = %(d)s
                    AND b.session_time = %(t)s)
           AND {_OCCUPY_START} < me.occupy_end
           AND me.occupy_start < {_OCCUPY_END}
         ORDER BY b.session_date, b.session_time
         LIMIT 1
        """,
        {"mmg": mmg_id, "d": session_date, "t": session_time},
    )
    return cur.fetchone()


def conflicting_gm(
    cur, gm_ids: list[int], mmg_id: int, session_date, session_time
) -> int | None:
    """gm_ids 裡任何一位在這段時間有沒有別的（不同場次的）已成立預約，
    回傳第一個撞期的 user id；都沒撞回傳 None。

    佔用區間的定義與包廂那支完全相同（見 _OCCUPY_*）：主持人一樣要在場地
    佈置與還原的時間裡待著，不是遊戲結束就能立刻去帶下一場。

    排除條件是「跟候選場次完全相同的 (mmg_id, session_date, session_time)」，
    不是「這位主持人」本身——同一場遊戲底下各序位掛的是同一位主持人，
    那是同一件事被記錄很多次，不是這位主持人被排了兩場不同的戲。
    """
    # functions 表的 conflict_gm。關掉的店等於「同一位主持人可以同時帶
    # 兩場」——那不是一個真實的營運模式，所以預設開著；留成可關是因為
    # 卡場條件本來就是每家店不同的那一組設定。
    if not functions.gm_conflict_on(cur):
        return None

    # ★ single 模式**不看紀錄上掛了誰**。
    #
    #   這家店只有一位主持人，任何一場已成立的場次都佔著她的時間——包括
    #   那些因為劇本沒替角色取名字、gm{n}_user_id 全是 null 的紀錄。照
    #   gm{n}_user_id 比對的話那些紀錄一筆都對不上，而這種店沒有包廂概念、
    #   劇本卡場也還沒實作，主持人卡場是**唯一**的保護——它安靜地失效的
    #   症狀是同一個時間被訂走兩場不同的戲，要等到當天才會發現。
    single = functions.gm_mode(cur) == "single"
    if single:
        who_join = "JOIN users u ON u.id = %(gms)s"
        gms = functions.SINGLE_GM_USER_ID
    else:
        if not gm_ids:
            return None
        who_join = (
            "JOIN users u ON u.id = ANY(%(gms)s)\n"
            "                      AND u.id IN (b.gm1_user_id, b.gm2_user_id,\n"
            "                                   b.gm3_user_id, b.gm4_user_id)"
        )
        gms = gm_ids
    cur.execute(
        f"""
        WITH me AS (
            SELECT (%(d)s + %(t)s)::timestamp - (ready_time_cost * interval '1 hour') AS occupy_start,
                   (%(d)s + %(t)s)::timestamp
                     + ((period + reset_time_cost) * interval '1 hour') AS occupy_end
              FROM mmg WHERE id = %(mmg)s
        )
        SELECT u.id
          FROM booking_record b
          JOIN mmg m2 ON m2.id = b.mmg_id
          {who_join}
          CROSS JOIN me
         WHERE b.status = 'booked'
           AND NOT (b.mmg_id = %(mmg)s
                    AND b.session_date = %(d)s
                    AND b.session_time = %(t)s)
           AND {_OCCUPY_START} < me.occupy_end
           AND me.occupy_start < {_OCCUPY_END}
         LIMIT 1
        """,
        {"gms": gms, "mmg": mmg_id, "d": session_date, "t": session_time},
    )
    row = cur.fetchone()
    return row[0] if row else None


def find_conflicts(cur, booking_id: int) -> dict:
    """這筆預約有沒有撞到已經成立的場次。分成兩種，因為它們是兩件事：

    room —— 這齣戲要用的那間「包廂」在這段時間已經被佔著了。包廂才是
        實體資源，一次只能開一場，跟是哪一齣戲、誰主持都無關。這條規則
        跟玩家預訂時擋人的 has_room_conflict() 是同一條。

    gms —— 這筆的某位「主持人」在這段時間已經被別的成立場次佔住。
        不限同一個劇本：他可能被指定去帶另一齣戲，時間一樣會撞。

    兩者可以各自成立，也可以同時發生。分開回傳讓呼叫端決定各自要怎麼
    處置——目前是包廂衝突可以放行、主持人自己撞期不能放行。

    佔用區間含佈置與還原時間，與 has_room_conflict／conflicting_gm
    用的是同一組定義（_OCCUPY_*）。三支必須一致：一支說可以、另一支說
    不行，畫面就會自相矛盾。

    ★ 「一致」包含**功能表**，不只佔用區間（2026-09-13 補）。這支原本
      無條件查兩種衝突，而另外兩支早就會看 conflict_room／conflict_gm。
      關掉包廂卡場的店（例如一人店，所有戲共用同一個 room_id）因此會被
      一堆它自己說不在乎的「包廂撞期」洗版——警告被洗到失去意義，比沒有
      警告更糟：人會學會直接按過去，連真的那一次也按過去。

      主持人那邊跟著 conflicting_gm 的同一條規則：single 模式看的是店主
      本人，不是紀錄上掛了誰（劇本沒替角色取名字時那些欄位是空的）。

    都只看 status = 'booked'。同一個時段有好幾組在排隊是正常的（那正是
    序位的意義），真正的衝突是「已經成立的那一場把資源佔住了」。
    """
    cur.execute(
        """
        SELECT b.mmg_id, b.session_date, b.session_time,
               b.gm1_user_id, b.gm2_user_id, b.gm3_user_id, b.gm4_user_id
          FROM booking_record b WHERE b.id = %s
        """,
        (booking_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise BookingError(f"預約 id={booking_id} 不存在")

    mmg_id, session_date, session_time = row[0], row[1], row[2]
    gm_ids = [h for h in row[3:7] if h is not None]

    # 候選場次自己的佔用區間，跟另外兩支用同一個定義。
    me_cte = """
        WITH me AS (
            SELECT room_id,
                   (%(d)s + %(t)s)::timestamp - (ready_time_cost * interval '1 hour') AS occupy_start,
                   (%(d)s + %(t)s)::timestamp
                     + ((period + reset_time_cost) * interval '1 hour') AS occupy_end
              FROM mmg WHERE id = %(mmg_id)s
        )
    """
    overlap = f"""
           AND NOT (b2.mmg_id = %(mmg_id)s
                    AND b2.session_date = %(d)s
                    AND b2.session_time = %(t)s)
           AND {_OCCUPY_START.replace('b.', 'b2.')} < me.occupy_end
           AND me.occupy_start < {_OCCUPY_END.replace('b.', 'b2.')}
    """
    params = {"booking_id": booking_id, "mmg_id": mmg_id,
              "d": session_date, "t": session_time}

    # ── 包廂被兩場戲同時佔著 ──
    room = []
    if functions.room_conflict_on(cur):
        cur.execute(
            f"""
            {me_cte}
            SELECT DISTINCT m2.name, b2.session_date, b2.session_time
              FROM booking_record b2
              JOIN mmg m2 ON m2.id = b2.mmg_id
              CROSS JOIN me
             WHERE b2.status = 'booked'
               AND b2.id <> %(booking_id)s
               AND m2.room_id = me.room_id
               {overlap}
             ORDER BY b2.session_date, b2.session_time
            """,
            params,
        )
        room = [
            {"mmg_name": r[0], "session_date": r[1].isoformat(),
             "session_time": r[2].strftime("%H:%M")}
            for r in cur.fetchall()
        ]

    # ── 某位主持人同時段已經有場 ──
    #
    # single 模式不看 b2 的 gm 欄位，理由與 conflicting_gm 完全相同：
    # 這家店只有一位主持人，任何一場已成立的場次都佔著她的時間，跟那筆
    # 紀錄上有沒有寫她的 id 無關（劇本沒替角色取名字時那些欄位是空的）。
    gms = []
    if functions.gm_conflict_on(cur):
        if functions.gm_mode(cur) == "single":
            who_join = "JOIN users u ON u.id = %(gm_ids)s"
            who = functions.SINGLE_GM_USER_ID
        else:
            who_join = (
                "JOIN users u ON u.id = ANY(%(gm_ids)s)\n"
                "                          AND u.id IN (b2.gm1_user_id, b2.gm2_user_id,\n"
                "                                       b2.gm3_user_id, b2.gm4_user_id)"
            )
            who = gm_ids
        if who:
            cur.execute(
                f"""
                {me_cte}
                SELECT DISTINCT u.id, u.name, m2.name, b2.session_date, b2.session_time
                  FROM booking_record b2
                  JOIN mmg m2  ON m2.id = b2.mmg_id
                  {who_join}
                  CROSS JOIN me
                 WHERE b2.status = 'booked'
                   AND b2.id <> %(booking_id)s
                   {overlap}
                 ORDER BY b2.session_date, b2.session_time
                """,
                {**params, "gm_ids": who},
            )
            gms = [
                {
                    "gm_id": r[0], "gm_name": r[1], "mmg_name": r[2],
                    "session_date": r[3].isoformat(),
                    "session_time": r[4].strftime("%H:%M"),
                }
                for r in cur.fetchall()
            ]

    return {"room": room, "gms": gms}


def _validate_gm_choice(cur, mmg_id: int, gm_user_ids: list[int | None]) -> None:
    """確認玩家挑的主持人真的會帶這個角色。

    這條規則資料庫擋不了：booking_record.gm1_user_id 的外鍵只保證「是一個
    存在的使用者」，不保證「這個人在 mmg.gm1_user 的名單裡」。跨表的成員
    檢查要寫成約束得用 trigger，不划算，所以在這裡擋。

    ★ 三種主持人模式在這裡的差別（functions 表的 gm_mode）：

        assign  每個有名字的角色都必須指定，而且要在該角色的名單裡（現況）
        option  可以不指定；有指定的還是要在名單裡
        single  完全不驗。那個模式下主持人不是呼叫端挑的，是系統依組態
                填進去的（見 create_booking），驗名單只會變成「店家忘記把
                自己加進每一齣戲的名單」就全店訂不了。
    """
    mode = functions.gm_mode(cur)
    if mode == "single":
        return
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
            if mode == "option":
                continue      # 「不指定」是合法的選擇，不是漏填
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
    detailed_conflict: bool = False,
    allow_closed_slot: bool = False,
    day_type: str | None = None,
) -> BookingResult:
    """建立一筆預約，回傳它的順位。

    gm_user_ids 是四個位置的主持人 user id，沒有的角色放 None。

    day_type 不給就依日期推（週六日＝假日場）。玩家那條路一律不給；只有
    場次管理新增時，管理員可能當場就指定（例如國定假日的週三）。

    ★ 刻意**不收訂金與備註**。一列剛誕生時這兩欄一定是「還沒收」與「沒有」：
      訂金要等主持人確認完才收（流程是 待確認 → 待收訂金 → 成立），備註是
      主持人／管理員的欄位。管理員代訂時先收了錢、或要留一句話，是建立之後
      由 bookings_admin.update() 寫進去的——那條路會跑轉態規則與應收金額
      檢查，這裡不會。
      這兩個參數原本存在（2026-08-26 設計成「唯一的寫入口」時一併收下），
      而玩家那支路由把請求內容原封不動轉手進來，等於任何人手打一個
      {"deposit": 99999} 就能在自己的預約上記一筆店家沒收到的錢。
      2026-09-12 移除。

    request_id 是冪等鍵，應該由前端在玩家「按下預約」當下產生，該次意圖的
    所有重試都沿用同一組。這裡允許不給（自動產生一組），只是為了讓腳本與
    測試方便；**正式的預約路徑一定要由呼叫端傳入**，自動產生等於每次重試
    都是新的意圖，冪等保護會失效。
    """
    request_id = request_id or str(uuid.uuid4())
    gm_ids = [i for i in gm_user_ids if i is not None]

    # ★ 排期檢查放在最前面，而且一定要在後端做。
    #
    #   前端會把不開放的日期反白、時間下拉只給得選的——但那是體驗，不是
    #   防線。任何人都能直接打這支 API 送一個沒開放的時間，日曆畫得再對
    #   也擋不住。這裡是最後一道，所以它必須存在，而且必須早於容量與
    #   衝突檢查：一個根本沒開放的時段，去談「還有幾個位子」是沒意義的。
    #
    #   跟前端共用 db/schedule.py 同一支判斷，不各自實作一份——兩邊只要
    #   差一點，使用者就會看到「畫面上選得到卻訂不下去」。
    # 未上架或已下架的劇本一律訂不了。玩家的清單裡本來就沒有它，但清單
    # 是畫面，這裡是防線——舊分頁、書籤、或直接打 API 都繞得過畫面。
    now = listing.now_taipei(cur)
    cur.execute(
        f"SELECT EXISTS (SELECT 1 FROM mmg WHERE id = %(id)s AND {listing.VISIBLE_SQL})",
        {"id": mmg_id, "now": now},
    )
    if not cur.fetchone()[0]:
        raise BookingError("此劇本目前未開放預約")

    if allow_closed_slot:
        # 管理員在「場次管理」新增：不問排期，連過去的時間都可以——那是在
        # 記錄已經發生的事（事後補登、電話預約的特殊場），不是在賣位子。
        # 容量與撞期檢查照樣跑，那兩道擋的是超賣與主持人分身，跟排期無關。
        #
        # ★ 這個參數從 2026-08-29 加入起就沒有任何呼叫端，是設計了但沒接上的
        #   功能；原本這裡的註解說「只有 role 1 走得到（見 app.py）」，實際上
        #   連 role 1 也走不到。2026-09-12 接到 bookings_admin.create()。
        pass
    else:
        # 先單獨判斷「已經過去」，只是為了給一句說得清楚的錯誤——真正的
        # 閘門仍然是下面的 is_open（它經過 schedule._bookable，同一條規則）。
        # 會走到這裡的真實情境是：玩家把頁面開著，選了今天 19:00，拖到
        # 19:01 才按送出。回「此時間未開放」會讓他以為這個時段本來就沒開。
        if datetime.combine(session_date, session_time) <= now:
            raise BookingError("這個時段已經過了，請重新選擇")
        if not schedule.is_open(cur, mmg_id, session_date, session_time):
            raise BookingError("此時間未開放")

    # ★ single 模式：主持人不是呼叫端挑的。店裡只有一位主持人（functions 表
    #   的 gm_mode = single），所以這裡直接把有名字的角色全部填成他，並且
    #   當場標成「已確認」——後面每一段判斷「還在等誰確認」的程式碼因此
    #   完全不需要知道 single 這回事。
    #
    #   固定用 users.id = 1：那家店的第一個帳號就是店主本人。案主明確承擔
    #   這個風險（2026-09-11）：「你不用擔心 DB 搬移意外，要真的發生我來改正」。
    gm_confirmed = [False, False, False, False]
    if functions.gm_mode(cur) == "single":
        cur.execute(
            "SELECT gm1_name, gm2_name, gm3_name, gm4_name FROM mmg WHERE id = %s",
            (mmg_id,),
        )
        names = cur.fetchone() or (None, None, None, None)
        gm_user_ids = [functions.SINGLE_GM_USER_ID if n else None for n in names]
        gm_confirmed = [n is not None for n in names]

        # ★ 卡場要鎖、要查的那個人，跟「紀錄上掛了誰」不是同一件事。
        #
        #   這家店只有一位主持人，她的時間被佔住跟劇本有沒有替角色取名字
        #   完全無關。沒取名的劇本上面那行會算出四個 null，拿它去查等於
        #   _lock_gms 不鎖、conflicting_gm 第一行就 return None——**卡場
        #   保護安靜地整個不存在**，而那是這種店唯一防止自己被重複預約的
        #   機制（沒有包廂概念、劇本卡場尚未實作）。
        #
        #   症狀會是「同一個時間被訂走兩場不同的戲」，而且沒有任何錯誤
        #   訊息，要等到當天才會發現。所以這裡不看名字，直接就是店主。
        gm_ids = [functions.SINGLE_GM_USER_ID]

    # 劇本卡場（同一齣戲同時間只能有一場，跟包廂、主持人同級的硬擋）
    # **尚未實作**。功能表裡有這個鍵、值固定 off，被打開就當場失敗——
    # 默默不生效的話，店家會以為自己開了保護，而系統照樣讓兩場同時開。
    if functions.script_conflict_on(cur):
        raise BookingError(
            "劇本卡場（conflict_script）尚未實作，請把 functions 表裡那一列改回 off"
        )

    day_type = day_type or default_day_type(session_date)
    if day_type not in DAY_TYPES:
        raise BookingError(f"未知的場次類型「{day_type}」")
    # 玩家那條路其實走不到這裡會失敗的情況：劇本存檔時已經確認過「開放的
    # 日子都有訂金」（check_deposit_coverage）。會擋到的是場次管理新增——
    # 那條路不問排期，可以開在劇本根本沒開放的日子上。不在這裡擋的話，
    # 這筆會帶著一個不存在的應收金額進入待收訂金，而且之後連劇本都存不了
    # （coverage 會因為它而失敗）。
    require_deposit_set(cur, mmg_id, day_type, creating=True)

    _lock_slot(cur, mmg_id, session_date, session_time)
    _lock_gms(cur, gm_ids)
    _validate_gm_choice(cur, mmg_id, gm_user_ids)

    # waitlist_limit 就是這個時段的總容量，不是「第 1 序位之外還能排幾組」。
    # 設 3 就是最多 3 組，第 4 組會被擋下。
    #
    # 計數的基準是「沒有取消」，不是「還沒成立」——已成立的那一組同樣佔著
    # 一個位子。這是刻意的：容量要擋的是「這個時段總共排了幾組人」，一組
    # 已經成立的預約不會因為成立了就不佔位。
    #
    # 衝突檢查的兩支自己會去讀 period／ready／reset，這裡不必先算好時間
    # 區間再傳進去——那三個值都在資料庫，繞出來一趟只是多一個會不同步的
    # 地方。
    cur.execute("SELECT name, waitlist_limit FROM mmg WHERE id = %s", (mmg_id,))
    mms_name, waitlist_limit = cur.fetchone()
    capacity = waitlist_limit

    clash = has_room_conflict(cur, mmg_id, session_date, session_time)
    if clash is not None:
        # ★ 預設不講是被什麼撞到。這支同時服務玩家與管理員兩條路徑，
        #   而玩家不該知道包廂裡在演什麼、誰訂的、演到幾點——那是別的
        #   客人的預約。管理員要處理衝突，才傳 detailed_conflict=True。
        if not detailed_conflict:
            raise BookingError("本時段有衝突場次，請改選其他時間")
        other_name, other_d, other_t, other_period, other_reset = clash
        raise BookingError(
            f"包廂在這段時間已經排了《{other_name}》"
            f"（{other_d} {other_t:%H:%M} 起 {other_period:g} 小時"
            f"，另需 {other_reset:g} 小時還原場地），時段衝突"
        )

    conflicting = conflicting_gm(cur, gm_ids, mmg_id, session_date, session_time)
    if conflicting is not None:
        cur.execute("SELECT name FROM users WHERE id = %s", (conflicting,))
        gm_name = cur.fetchone()[0]
        raise BookingError(f"{gm_name}在這個時間有場次無法預定")

    cur.execute(
        "SELECT COUNT(*) FROM booking_record "
        "WHERE mmg_id = %s AND session_date = %s AND session_time = %s "
        "  AND status <> 'cancelled'",
        (mmg_id, session_date, session_time),
    )
    current = cur.fetchone()[0]

    # ★ 狀態用推導的，不是靠欄位的預設值。assign＋收訂金（步經徑）推出來
    #   就是 gm_confirm，跟原本的 DEFAULT 一樣；single＋不收訂金的店則是
    #   一建立就 booked，不會卡在一個那家店根本沒有的狀態上。
    status = derive_status(
        cur, pending=pending_gm_count(gm_user_ids, gm_confirmed,
                                      mode=functions.gm_mode(cur))
    )
    # ── 先查再寫（2026-09-14 改）──────────────────────────────────
    #
    # 原本的順序是「先 INSERT、再檢查容量、滿了才拋錯」，把退掉那一筆交給
    # 呼叫端（網頁與管理員兩條路都有 conn.rollback()）。那是一個**隱性約定**：
    # 每個新的呼叫點都得記得失敗要退。LINE bot 那一條沒記得，結果回覆說
    # 「已經有場次了」，資料庫卻多了一筆。
    #
    # 要不要拒絕，在寫入之前就已經算得出來——current 是上面數的，而且在
    # _lock_slot 裡。INSERT 原本擺在前面，唯一的理由是讓「同一次意圖的重試」
    # 先被攔下來；那件事用 request_id 先查一樣做得到。所以改成：
    #
    #     先查是不是重試 → 是新的才看容量 → 沒滿才寫
    #
    # 對原本兩個呼叫點，**最後的資料庫結果完全相同**，差別只在「滿了的時候
    # 根本不寫」——它們的 rollback 從此沒有東西可退，但留著無害。
    cur.execute("SELECT id FROM booking_record WHERE request_id = %s", (request_id,))
    existing = cur.fetchone()

    if existing is not None:
        # 同一次意圖的重試。回報成功並附上原本那筆，不是失敗：玩家只按了一次，
        # 中間是網路出問題，不該讓他以為預約沒成立而再按一次。
        #
        # ★ 刻意**不看容量**：一個已經成立的預約即使現在時段滿了，重試時仍該
        #   回報它原本的狀態，而不是因為「滿了」被拒絕。（原本 INSERT 擺在
        #   容量檢查前面就是為了這一點，現在由這個分支保證。）
        booking_id = existing[0]
        already_existed = True
    else:
        if current >= capacity:
            # ★ 容量 1 的時段沒有「排隊」這回事，訊息要跟著換。
            #   原本一律講「已額滿（最多 N 組）」：對容量 1 的店家，那是把一個
            #   內部欄位的值講給玩家聽，而且「已額滿」暗示有人在排、等別人取消
            #   就有機會——但這種店沒有候補制度，那個時段就是被訂走了，等下去
            #   沒有意義。前端的 slotMessage 用同一條規則（看 slot.capacity）。
            if waitlist_limit == 1:
                raise BookingError("這個時段已經被預約了，請改選其他時間")
            raise BookingError(
                f"《{mms_name}》{session_date} {session_time} 已額滿"
                f"（最多 {waitlist_limit} 組）"
            )
        cur.execute(
            """
            INSERT INTO booking_record
                (mmg_id, player_id, session_date, session_time, day_type, status,
                 gm1_user_id, gm1_confirmed, gm2_user_id, gm2_confirmed,
                 gm3_user_id, gm3_confirmed, gm4_user_id, gm4_confirmed, request_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (request_id) DO NOTHING
            RETURNING id
            """,
            (mmg_id, player_id, session_date, session_time, day_type, status,
             gm_user_ids[0], gm_confirmed[0], gm_user_ids[1], gm_confirmed[1],
             gm_user_ids[2], gm_confirmed[2], gm_user_ids[3], gm_confirmed[3],
             request_id),
        )
        row = cur.fetchone()
        if row is None:
            # 保險：同一個 request_id 在上面那句 SELECT 與這句 INSERT 之間被別的
            # 交易寫進去了。同一時段有 _lock_slot 序列化，照理不會發生——但
            # 真的發生時要照樣回報成功（它就是一次重試），不是撞 UNIQUE 炸掉。
            cur.execute("SELECT id FROM booking_record WHERE request_id = %s", (request_id,))
            booking_id = cur.fetchone()[0]
            already_existed = True
        else:
            booking_id = row[0]
            already_existed = False

    position = get_position(cur, booking_id)
    # 任何預約寫入都清空 /slot 的快取。理由與粗粒度的取捨見 db/slot_cache.py：
    # has_room_conflict 是跨劇本、跨時間區間的，算不出「該清哪些鍵」，
    # 而讀取遠多於寫入，所以清空的代價自動被攤平。
    #
    # ★ 放在這裡（函式內、commit 之前）而不是呼叫端，是為了讓未來新增的
    #   呼叫路徑自動被涵蓋。代價是清空到 commit 之間有一個很短的窗口，
    #   由 slot_cache 的 TTL 收尾。
    slot_cache.clear()
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


def pending_gm_count(gm_user_ids, gm_confirmed, *, mode: str) -> int:
    """還在等幾位主持人確認。狀態推導唯一的輸入。

    「等」的定義依主持人模式而不同，但算法只有一種：**已指定、還沒確認的
    角色數**。三種模式自然落在同一條式子上：

        assign  指定了就要確認 → 通常 > 0，全員確認後變 0
        single  建立時就指定好並標成已確認 → 恆為 0
        option  沒指定的角色是「不指定」，不列入計算 → 可能一開始就是 0

    ★ assign 模式多一個守衛：一位都沒指派時回 1，不是 0。一筆還沒排主持人
      的預約不該被當成「大家都確認了」而自己往下一個狀態跳（後台新增時
      可以不填主持人，玩家那條路則被 _validate_gm_choice 擋著）。
      這一條是既有行為，不是新規則——原本寫在 bookings_admin.update 裡。
    """
    assigned = [(u, c) for u, c in zip(gm_user_ids, gm_confirmed) if u]
    if mode == "assign" and not assigned:
        return 1
    return sum(1 for _u, confirmed in assigned if not confirmed)


def derive_status(cur, *, pending: int) -> str:
    """一筆預約現在應該是什麼狀態。**所有轉移點都走這一支。**

        還在等主持人確認     → gm_confirm
        沒有人要等了 + 收訂金 → gm_reviewed（待收訂金）
        沒有人要等了 + 不收   → booked（直接成立）

    這支取代了原本散在六個地方的寫死值（建立時的預設、主持人確認完之後、
    後台勾完確認之後…）。散著的時候，一家不收訂金的店會在「主持人確認完」
    那一步掉進「待收訂金」，而畫面上沒有地方可以付——而且管理員救不回來。
    """
    if pending > 0:
        return "gm_confirm"
    return "gm_reviewed" if functions.deposit_required(cur) else "booked"


def get_booking(cur, booking_id: int) -> dict | None:
    """單筆預約的完整資料，含玩家與劇本名稱。

    booking_cost 是**這一筆**該收的訂金（依場次類型取平日或假日），不是
    劇本的平日訂金——通知信與畫面要的都是前者。
    """
    cur.execute(
        f"""
        SELECT b.id, b.mmg_id, m.name, b.player_id, u.name, u.email,
               b.session_date, b.session_time, b.status, b.deposit,
               {DUE_SQL}, b.note, b.created_at,
               b.gm1_user_id, b.gm2_user_id, b.gm3_user_id, b.gm4_user_id,
               b.day_type
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
        "day_type": row[17],
    }


# 玩家自己的場次清單分頁。跟主持人／管理員一樣按狀態分，但用玩家看得懂
# 的說法：他不需要知道「待收訂金」是店家的動作，他要知道的是「該去付了」。
# 五個狀態的中文，**玩家與管理員共用同一份**。
#
# 原本是兩份：玩家看「等待主持人確認／等待支付訂金／預約完成」，管理員看
# 「待主持確認／待收訂金／已成立」。指的是同一個狀態，只是各寫各的，久了
# 就會漂移——而這兩邊的人是會對話的（「我這場是不是已成立？」「系統上寫
# 預約完成」），用詞不一樣會讓同一件事聽起來像兩件事。
#
# 選字以玩家那一版為基準：管理員讀得懂玩家的說法，反過來不一定
# （「待收訂金」對玩家是站在店家立場講話）。booked 取兩邊的交集「預約成立」。
#
# ★ 這一份是「頁籤標題」。另有一份 STATUS_LABEL（本檔下方）是主持人視角
#   的說法（「等待其他主持確認」——對主持人而言關鍵是還缺別人，不是缺自己），
#   那是不同的資訊，刻意沒有併進來。
#
# ★ 共用同一個物件是刻意的——之後只要有人改這裡，兩個畫面一定同時變。
#   哪天真的需要分開講（例如管理員要更精簡的字），就在這裡拆成兩份，
#   而不是在畫面上各自蓋掉。
TAB_LABEL = {
    "gm_confirm": "等待主持人確認",
    "gm_reviewed": "等待支付訂金",
    "booked": "預約成立",
    "ended": "已結束",
    "cancelled": "已取消",
}

# 玩家的頁籤就是這五個狀態。
PLAYER_TABS = TAB_LABEL


def reachable_tabs(cur) -> dict[str, str]:
    """這家店的設定下，哪些狀態真的到得了。

    single 模式的待確認主持人數恆為 0，所以 gm_confirm 推導不出來；
    不收訂金的店不會停在 gm_reviewed。把到不了的頁籤留在畫面上，店家
    會一直點進去看到空的，然後問「我又不收訂金，為什麼有『等待支付
    訂金』」——那是設定沒有反映到畫面上，不是她看錯。

    ★ 但「現在真的有紀錄」的狀態一律保留，就算它已經到不了。設定是可以
      改的，而改之前留下的那些場次不能因此從管理畫面上消失——看不見的
      資料比多一個空頁籤危險得多。這也是唯一能把那些場次救回來的入口。

    ★ 這支只決定「畫面上列出哪幾個」。list_tab 仍然接受全部五個，直接
      用網址指定一個沒列出來的頁籤照樣查得到——把它擋掉只會讓上面那條
      救援路徑斷掉。
    """
    out = dict(TAB_LABEL)
    unreachable = set()
    if functions.gm_mode(cur) == "single":
        unreachable.add("gm_confirm")
    if not functions.deposit_required(cur):
        unreachable.add("gm_reviewed")
    if not unreachable:
        return out
    cur.execute(
        "SELECT DISTINCT status FROM booking_record WHERE status = ANY(%s)",
        (sorted(unreachable),),
    )
    has_rows = {r[0] for r in cur.fetchall()}
    for key in unreachable - has_rows:
        out.pop(key, None)
    return out

# 已結束與已取消看最近的，其餘看最早的——待處理的先做舊的，已完結的
# 通常是查最近那幾筆。跟主持人與管理員的清單同一個原則。
_PLAYER_DESC_TABS = ("ended", "cancelled")


def list_player_tab(
    cur, player_id: int, tab: str, *, start: int = 1, filters: dict | None = None
) -> dict:
    """某位玩家在某個狀態下的預約，一次一頁。

    序位即時算出來，不存欄位——玩家最在意的就是「我排第幾」，而那個值會
    因為前面的人取消而改變。存起來就得在每次取消時回頭更新所有後面的人，
    漏一次就永遠錯。

    篩選只開放劇本名稱與日期時間。預定者一定是他自己，角色是主持人那邊
    的概念，狀態則由頁籤決定——多給的選項只會讓人試出空集合。
    """
    if tab not in PLAYER_TABS:
        raise BookingError(f"未知的頁籤「{tab}」")

    allowed = {k: v for k, v in (filters or {}).items()
               if k in ("mmg_name", "session_date", "session_time")}
    where, fp = filter_clauses(allowed)
    order = ("b.session_date DESC, b.session_time DESC"
             if tab in _PLAYER_DESC_TABS else "b.session_date, b.session_time")

    def item(r):
        return {
            "id": r[0], "mmg_name": r[1], "mmg_url": r[2],
            "session_date": r[3].isoformat(), "session_time": r[4].strftime("%H:%M"),
            "status": r[5], "status_label": PLAYER_TABS.get(r[5], r[5]),
            "deposit": r[6], "booking_cost": r[7], "note": r[8],
            "position": r[9],
            "gm_names": [n for n in r[10:14] if n],
            # 前端決定要不要顯示「第 N 序位」用的：容量 1 的時段沒有排隊，
            # 講序位只會讓人以為後面還有第 2、第 3。放在最後一欄，
            # 不動任何既有欄位的位置索引。
            "waitlist_limit": r[14],
            "day_type": r[15],
        }

    # booking_cost 是這一筆該收的訂金，依場次類型取——見 DUE_SQL。
    return paged(
        cur,
        f"""
        SELECT b.id, m.name, m.url, b.session_date, b.session_time, b.status,
               b.deposit, {DUE_SQL}, b.note,
               CASE WHEN b.status = 'cancelled' THEN NULL ELSE (
                   SELECT COUNT(*) + 1 FROM booking_record x
                    WHERE x.mmg_id = b.mmg_id
                      AND x.session_date = b.session_date
                      AND x.session_time = b.session_time
                      AND x.status <> 'cancelled'
                      AND x.created_at < b.created_at
               ) END AS position,
               u1.name, u2.name, u3.name, u4.name,
               m.waitlist_limit, b.day_type
          FROM booking_record b
          JOIN mmg m ON m.id = b.mmg_id
          LEFT JOIN users u1 ON u1.id = b.gm1_user_id
          LEFT JOIN users u2 ON u2.id = b.gm2_user_id
          LEFT JOIN users u3 ON u3.id = b.gm3_user_id
          LEFT JOIN users u4 ON u4.id = b.gm4_user_id
         WHERE b.player_id = %(player)s
           AND b.status = %(tab)s
           {where}
         ORDER BY {order}
         LIMIT %(limit)s OFFSET %(offset)s
        """,
        {"player": player_id, "tab": tab, "offset": start - 1, **fp},
        item,
    )


def record_deposit(cur, booking_id: int, amount: int) -> dict:
    """記錄實際收到的訂金金額。

    通知玩家繳訂金、確認款項是否已到帳，是管理員在系統外做的事；這裡
    只負責把金額寫進去。狀態要不要跟著跳到 booked，在同一句 UPDATE 裡
    讓資料庫判斷，不由呼叫端另外決定：

        目前 status = 'gm_reviewed'，且新金額 >= 這一筆的應收訂金
        → 這句 UPDATE 一併把 status 轉成 'booked'

    應收訂金依這一筆的場次類型取劇本的平日或假日訂金（DUE_SQL）。

    只在 gm_reviewed 這個狀態才會自動轉——主持人都還沒審過的場次，
    就算訂金收滿了也不該直接跳過審核變成成立。

    ★ 應收訂金是空的時候直接擋下，不再默默不轉。原本的行為是比較結果
      恆為 NULL、CASE 不會命中、狀態停在原地——金額寫進去了、畫面說
      已儲存，這筆卻永遠等不到成立，沒有任何人會發現。現在劇本存檔與
      建立預約都會先確認金額有設定（check_deposit_coverage、
      require_deposit_set），走到這裡還是空的，代表有一條路漏掉了，
      那要讓人看見。

    用 >=（達到即可）而不是嚴格等於：多收一點不該卡住轉態，也允許
    分兩次記錄金額湊足（先記一部分、之後補到滿額）。如果你要的是
    金額必須跟劇本要求分毫不差才轉態，這裡改成 = 即可。
    """
    # ★ 不收訂金的店直接拒絕，不是靠呼叫端記得問。
    #
    #   目前兩個呼叫端都有問（bookings_admin.update 與玩家那條路），但
    #   這是一支 public 函式——下一個呼叫點忘記問的時候，症狀是「這家店
    #   明明不收訂金，卻有一筆預約因為訂金而改了狀態」，而那跟設定完全
    #   對不起來，會查很久。守在這裡的話它當場就說得出原因。
    if not functions.deposit_required(cur):
        raise BookingError("這家店不收訂金（functions 表 deposit=off），不能記錄訂金")

    # 先鎖住這一列再看它的狀態與應收金額：鎖在 UPDATE 之前拿，中間就
    # 不會有別人（主持人按確認）把狀態從 gm_confirm 推到 gm_reviewed，
    # 讓這裡看到的跟 UPDATE 當下的不一樣。
    cur.execute(
        f"""
        SELECT b.status, b.day_type, m.name, {DUE_SQL}
          FROM booking_record b JOIN mmg m ON m.id = b.mmg_id
         WHERE b.id = %s
           FOR UPDATE OF b
        """,
        (booking_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise BookingError(f"預約 id={booking_id} 不存在")
    status, day_type, mmg_name, due = row
    if status == "cancelled":
        raise BookingError("已取消的預約不能記錄訂金")
    if status == "gm_reviewed" and due is None and functions.deposit_required(cur):
        label = DAY_TYPE_LABEL[day_type]
        raise BookingError(
            f"《{mmg_name}》沒有設定{label}訂金，無法判斷這筆是否已收足。"
            f"請先到劇本管理補上（不收訂金請填 0）"
        )

    cur.execute(
        f"""
        UPDATE booking_record b
           SET deposit = %s,
               status = CASE
                          WHEN b.status = 'gm_reviewed'
                               AND %s >= (SELECT {DUE_SQL} FROM mmg m WHERE m.id = b.mmg_id)
                          THEN 'booked'
                          ELSE b.status
                        END
         WHERE b.id = %s AND b.status <> 'cancelled'
        RETURNING id
        """,
        (amount, amount, booking_id),
    )
    if cur.fetchone() is None:
        # ★ 走不到：上面那句 SELECT ... FOR UPDATE 已經確認這一列存在、不是
        #   已取消，而且鎖住它到交易結束，中間沒有別人動得了。原本這裡還會
        #   再查一次分辨「不存在」與「已取消」，那兩句在加了鎖之後永遠不會
        #   執行，所以拿掉了。
        #
        #   這一行留著是為了「哪天有人把上面的鎖或那段檢查拿掉」——那時這裡
        #   會大聲失敗，而不是默默沒寫進去。
        raise BookingError(f"預約 id={booking_id} 在寫入前被改掉了，請重新整理再試")
    # 待收訂金 → 已成立會改變 /slot 的答案：has_booked 是「這個時段有沒有
    # 已成立的組別」，而撞期判定只算 booked，所以連別的劇本的時段都會變。
    slot_cache.clear()  # 見 create_booking 上方的說明
    return get_booking(cur, booking_id)


def confirm_gm(cur, booking_id: int, gm_id: int) -> dict:
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
    # (gm{n}_user_id = %(gm)s) 在 SQL 三值邏輯下算出來是 NULL，
    # 不是 false，而 `false OR NULL` 還是 NULL——寫進 NOT NULL 欄位
    # 直接違反約束（實測撞過：NotNullViolation on gm4_confirmed）。
    cur.execute(
        """
        UPDATE booking_record
           SET gm1_confirmed = gm1_confirmed OR COALESCE(gm1_user_id = %(gm)s, false),
               gm2_confirmed = gm2_confirmed OR COALESCE(gm2_user_id = %(gm)s, false),
               gm3_confirmed = gm3_confirmed OR COALESCE(gm3_user_id = %(gm)s, false),
               gm4_confirmed = gm4_confirmed OR COALESCE(gm4_user_id = %(gm)s, false)
         WHERE id = %(id)s
           AND status = 'gm_confirm'
           AND %(gm)s IN (gm1_user_id, gm2_user_id, gm3_user_id, gm4_user_id)
           -- 這個人還沒確認過。少了這行，重複確認會靜靜地成功
           -- （OR 是冪等的，true OR true 還是 true），呼叫端拿到 200
           -- 卻什麼都沒發生——UI 上那一列早就變暗了，回一個假的成功
           -- 比回錯誤更難查。
           AND NOT (
                 COALESCE(gm1_user_id = %(gm)s AND gm1_confirmed, false)
              OR COALESCE(gm2_user_id = %(gm)s AND gm2_confirmed, false)
              OR COALESCE(gm3_user_id = %(gm)s AND gm3_confirmed, false)
              OR COALESCE(gm4_user_id = %(gm)s AND gm4_confirmed, false)
           )
        RETURNING
            (gm1_user_id IS NULL OR gm1_confirmed)
        AND (gm2_user_id IS NULL OR gm2_confirmed)
        AND (gm3_user_id IS NULL OR gm3_confirmed)
        AND (gm4_user_id IS NULL OR gm4_confirmed)
        """,
        {"gm": gm_id, "id": booking_id},
    )
    row = cur.fetchone()
    if row is None:
        # 沒更新到任何列。分辨是哪一種情況，回一個說得清楚的錯誤——
        # 三種都回同一句話的話，主持人看到「你不是指定的主持人」
        # 卻明明看得到這場，會不知道發生什麼事。
        cur.execute(
            """
            SELECT status,
                   COALESCE(%(gm)s IN (gm1_user_id, gm2_user_id,
                                         gm3_user_id, gm4_user_id), false),
                   COALESCE(gm1_user_id = %(gm)s AND gm1_confirmed, false)
                OR COALESCE(gm2_user_id = %(gm)s AND gm2_confirmed, false)
                OR COALESCE(gm3_user_id = %(gm)s AND gm3_confirmed, false)
                OR COALESCE(gm4_user_id = %(gm)s AND gm4_confirmed, false)
              FROM booking_record WHERE id = %(id)s
            """,
            {"gm": gm_id, "id": booking_id},
        )
        existing = cur.fetchone()
        if existing is None:
            raise BookingError(f"預約 id={booking_id} 不存在")
        status, is_gm, already = existing
        if not is_gm:
            raise BookingError("你不是這場預約指定的主持人")
        if already:
            raise BookingError("你已經確認過這場預約了")
        raise BookingError("這筆預約已經不在待確認狀態")

    if row[0]:
        # 全部確認完了 → 還在等的人數是 0，下一個狀態由 derive_status 決定：
        # 收訂金的店是「待收訂金」，不收的店直接成立。
        cur.execute(
            "UPDATE booking_record SET status = %s "
            "WHERE id = %s AND status = 'gm_confirm'",
            (derive_status(cur, pending=0), booking_id),
        )
    # 這一支目前不影響 /slot 的任何欄位（容量算「沒取消」、撞期算 booked，
    # 主持人確認兩者都不動）。還是清，是為了讓規則保持一句話：**寫
    # booking_record 就清**。payload 哪天多一個欄位時，不必回頭重新推一次
    # 哪些狀態轉換會影響它。
    slot_cache.clear()  # 見 create_booking 上方的說明
    return get_booking(cur, booking_id)


# 主持人視角的說法：**就是 TAB_LABEL，只改真的有主持人專屬資訊的那一個。**
#
# 原本這裡是獨立的第三份，五個狀態各寫各的，結果 gm_reviewed 寫成「等待訂金
# 支付」、booked 寫成「場次已預約完成」——跟 TAB_LABEL 講的是同一件事，只是
# 換了順序跟長度。那不是兩種說法，是兩份沒對齊的同一句話。
#
# 改成從 TAB_LABEL 展開之後，**沒有列在下面的狀態不可能再漂移**：改 TAB_LABEL
# 就兩邊一起變。要讓某個狀態對主持人講不一樣的話，就在下面多寫一行，而那一行
# 本身就是「這裡刻意不同」的宣告。
#
# 目前只有一個：gm_confirm 對主持人而言關鍵是還缺**別人**，不是缺自己——他自己
# 已經確認過了，所以他看到的是「等待其他主持確認」。這是他這個角色才需要知道
# 的事。
STATUS_LABEL = {
    **TAB_LABEL,
    "gm_confirm": "等待其他主持確認",
}


_GM_ROLE_NAME = """
    CASE WHEN b.gm1_user_id = %(gm)s THEN m.gm1_name
         WHEN b.gm2_user_id = %(gm)s THEN m.gm2_name
         WHEN b.gm3_user_id = %(gm)s THEN m.gm3_name
         WHEN b.gm4_user_id = %(gm)s THEN m.gm4_name
    END
"""

_GM_CONFIRMED = """
    CASE WHEN b.gm1_user_id = %(gm)s THEN b.gm1_confirmed
         WHEN b.gm2_user_id = %(gm)s THEN b.gm2_confirmed
         WHEN b.gm3_user_id = %(gm)s THEN b.gm3_confirmed
         WHEN b.gm4_user_id = %(gm)s THEN b.gm4_confirmed
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
        sql.append(f"AND {_GM_ROLE_NAME} ILIKE %(f_role)s")
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
        "where": f"AND b.status = 'gm_confirm' AND NOT COALESCE({_GM_CONFIRMED}, false)",
        "order": "b.session_date, b.session_time",
    },
    "confirmed": {
        "where": f"""AND (
                 b.status IN ('gm_reviewed', 'booked')
                 OR (b.status = 'gm_confirm' AND {_GM_CONFIRMED})
               )""",
        "order": "b.session_date, b.session_time",
    },
    "ended": {
        "where": "AND b.status = 'ended'",
        "order": "b.session_date DESC, b.session_time DESC",
    },
}


def gm_tab(
    cur,
    gm_id: int,
    tab: str,
    *,
    start: int = 1,
    filters: dict | None = None,
) -> dict:
    """撈主持人介面「某一個頁籤」的一頁資料。

    分頁與篩選都是當前頁籤的事，不影響另外兩個頁籤——所以它們是這支
    函式的參數，而不是 gm_dashboard() 的。使用者翻頁或按下篩選時，
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
            "id": r[0], "mmg_name": r[1], "mmg_url": r[2], "player_name": r[3],
            "session_date": r[4].isoformat(), "session_time": r[5].strftime("%H:%M"),
            "status": r[6], "status_label": STATUS_LABEL[r[6]], "role_name": r[7],
        }

    spec = _TABS[tab]
    return paged(
        cur,
        f"""
        SELECT b.id, m.name, m.url, u.name, b.session_date, b.session_time, b.status,
               {_GM_ROLE_NAME} AS role_name
          FROM booking_record b
          JOIN mmg m   ON m.id = b.mmg_id
          JOIN users u ON u.id = b.player_id
         WHERE %(gm)s IN (b.gm1_user_id, b.gm2_user_id, b.gm3_user_id, b.gm4_user_id)
           {spec["where"]}
           {where}
         ORDER BY {spec["order"]}
         LIMIT %(limit)s OFFSET %(offset)s
        """,
        {"gm": gm_id, "offset": start - 1, **fp},
        item,
    )


# 主持人「已確認場次」頁籤的狀態篩選選項，順序即畫面上的順序（跟著流程走）。
#
# ★ 由後端定義而不是前端寫死，因為**能篩出什麼是上面那條 WHERE 決定的**：
#   已確認頁籤收的就是 gm_reviewed、booked，以及自己已確認但整筆還是
#   gm_confirm 的那些。前端另外寫一份，就變成同一件事有兩個定義，而它們
#   會走鐘——實際上已經走鐘過：同一個狀態在篩選器下拉裡是「等待訂金支付」，
#   在清單列裡卻是「等待支付訂金」，兩邊各改各的沒人發現。
#
# 只有已確認頁籤篩狀態才有意義：待確認永遠是 gm_confirm、已結束永遠是
# ended，對它們篩狀態等於沒篩。
#
# 「全部」那一項不放這裡——它不是一個狀態，是「不要篩」，屬於前端的操作。
GM_FILTER_STATUSES = ("gm_confirm", "gm_reviewed", "booked")


def gm_dashboard(cur, gm_id: int) -> dict:
    """主持人介面初次載入：三個頁籤各拿第一頁，一支 API 打完。

    只給「剛打開頁面」用，刻意不收分頁與篩選參數——那兩件事都只作用於
    當前頁籤，走 gm_tab() 單獨拿即可，不需要為了改動一個頁籤而把
    另外兩個也重查一次。

    ★ 除了三個頁籤，還帶回 status_options。跟著這一支回而不是另開一支，
      理由與 /api/admin/users 把角色選項跟使用者清單一起回一樣：省一次
      請求，而且不會有「清單」與「下拉」來自不同時間點的問題。
    """
    out = {tab: gm_tab(cur, gm_id, tab) for tab in _TABS}
    out["status_options"] = [
        {"value": s, "label": STATUS_LABEL[s]} for s in GM_FILTER_STATUSES
    ]
    return out


# 往回看幾天。遊戲可能跨一個夜（晚上開場、隔天早上結束），但不會跨兩個，
# 所以兩天足以涵蓋任何一場「昨天開、今天才結束」的場次。
ENDED_LOOKBACK_DAYS = 2


def mark_ended_batch(cur, *, lookback_days = ENDED_LOOKBACK_DAYS) -> int:
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
           -- 只往回看幾天。★ 這道限制會在 job 連續數天沒跑時留下漏網之魚：
           -- 超出窗口的 booked 場次不會再被這支轉成 ended，而下面那支
           -- cancel_expired_batch 又刻意跳過 booked，於是它會永遠停在
           -- booked。run_daily() 因此回報 stale_booked，讓漏掉的場次至少
           -- 是看得見的，而不是安靜地卡著。
           --
           -- lookback_days 傳 None 就不設窗口，全部歷史一起處理——管理員
           -- 介面的「場次整理」走這條，用來手動補上漏跑的那幾天。
           AND (%(lookback)s::int IS NULL
                OR b.session_date >= (NOW() AT TIME ZONE 'Asia/Taipei')::date
                                     - %(lookback)s::int)
        """,
        {"lookback": lookback_days},
    )
    slot_cache.clear()  # 見 create_booking 上方的說明
    return cur.rowcount


def cancel_expired_batch(cur) -> int:
    """把「日期已經過去、卻還沒成立也沒取消」的預約批次轉成 cancelled。

    針對的是 gm_confirm 與 gm_reviewed：場次日期都過了還沒走完流程，那場
    就是沒發生。留著它們會讓待辦清單愈積愈多，也會讓容量計算把早就作廢的
    組別繼續算進去（容量看的是「沒取消」）。

    刻意跳過 booked：那些是真的有開的場次，該由 mark_ended_batch() 轉成
    ended，不是取消。也跳過 ended 與 cancelled，它們已經是終點。

    比的是「日期」不是「日期加時間」：一場昨天 23:00 的預約，今天 00:00
    跑的時候場次才過了一小時，但日期已經翻頁——沒人在那個時間點還會來確認
    它。用日期比對讓這條規則跟排程的節奏一致（每天 00:00 一次）。
    """
    cur.execute(
        """
        UPDATE booking_record
           SET status = 'cancelled'
         WHERE status NOT IN ('booked', 'cancelled', 'ended')
           AND session_date < (NOW() AT TIME ZONE 'Asia/Taipei')::date
        """
    )
    slot_cache.clear()  # 見 create_booking 上方的說明
    return cur.rowcount


# 候補的保存期限。玩家在加入一個「已經有人成立」的時段時被告知：正選沒
# 取消的話，這張候補三天後自動失效。這個天數就是那句話的依據，改這裡的
# 同時要改 booking.js 的 slotMessage()。
WAITLIST_EXPIRY_DAYS = 3


def expire_stale_waitlist_batch(cur, *, days: int = WAITLIST_EXPIRY_DAYS) -> int:
    """把「排在已成立場次後面、且掛太久」的候補批次轉成 cancelled。

    對應玩家看到的那句「若排定預約未取消，候補將於三天後自動失效」。
    在這支存在之前那是一張空頭支票——訊息這樣寫，但沒有任何東西在做。

    天數從**這張候補自己建立的時間**算起，不是從正選成立算起：那句話是
    在玩家按下預約的當下對他說的，他理解的三天當然從那一刻開始。

    只在「該時段真的有一組 booked」時才失效。正選後來取消了的話，這張
    候補實際上已經遞補上去，沒有理由讓它過期。

    跟 cancel_expired_batch 一樣跳過 booked／cancelled／ended。
    """
    cur.execute(
        """
        UPDATE booking_record w
           SET status = 'cancelled'
         WHERE w.status NOT IN ('booked', 'cancelled', 'ended')
           AND w.created_at < NOW() - (%s::int * interval '1 day')
           AND EXISTS (
                 SELECT 1 FROM booking_record p
                  WHERE p.mmg_id = w.mmg_id
                    AND p.session_date = w.session_date
                    AND p.session_time = w.session_time
                    AND p.id <> w.id
                    AND p.status = 'booked'
               )
        """,
        (days,),
    )
    slot_cache.clear()  # 見 create_booking 上方的說明
    return cur.rowcount


def count_stale_booked(cur, *, lookback_days = ENDED_LOOKBACK_DAYS) -> int:
    """已經結束、卻因為超出回看窗口而不會再被轉成 ended 的場次數。

    正常運作下永遠是 0。不是 0 就代表排程漏跑過，而那些場次靠自己不會
    再回到正軌——這個數字是用來讓那件事被看見的。
    """
    cur.execute(
        """
        SELECT COUNT(*) FROM booking_record b JOIN mmg m ON m.id = b.mmg_id
         WHERE b.status = 'booked'
           AND (b.session_date + b.session_time) AT TIME ZONE 'Asia/Taipei'
                 + (m.period * interval '1 hour') <= NOW()
           AND b.session_date < (NOW() AT TIME ZONE 'Asia/Taipei')::date - %s::int
        """,
        (lookback_days,),
    )
    return cur.fetchone()[0]


