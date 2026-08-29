"""
管理員介面「劇本管理」的資料存取層。

「有沒有真的改到東西」由這一層判斷，不是前端自己比對：存檔前先撈現值
逐欄比對，只有真的有差異才下 UPDATE，並回傳 changed，讓前端決定要跳
「已儲存」還是「無任何修改」。這個做法沿用自 zh-cn-to-tw 的
admin_utils/users.py。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from db.bookings import BookingError

# 台北是固定的 UTC+8，沒有日光節約，所以不需要 zoneinfo（那在某些精簡的
# 部署環境沒有時區資料庫可讀）。全系統一律以台北時間解讀日期。
_TAIPEI = timezone(timedelta(hours=8))


def _now_taipei() -> str:
    """現在（台北），精確到分。秒數對「上架時刻」沒有意義，留著只會讓
    管理員看到 14:03:27 這種讀不出用意的值。"""
    return datetime.now(_TAIPEI).strftime("%Y-%m-%dT%H:%M")

# 可編輯的欄位。id 不在裡面——主鍵不給改，改了等於換一齣劇本，
# 而所有 booking_record 都還指著舊的 id。
# 逐欄比對「有沒有真的改到東西」時要看哪些欄位。
#
# ★ 新增可編輯欄位時務必同步加進來。漏掉的欄位不會報錯，只會讓改動被
#   判定成「無任何修改」而不寫入——使用者改了、按了儲存、看到提示，
#   資料卻沒變。ready/reset/room_id 就是這樣漏過一次。
EDITABLE = (
    "name", "period", "ready_time_cost", "reset_time_cost",
    "price", "booking_cost", "players",
    "waitlist_limit", "status", "room_id",
    # 上下架日期。排期判斷的第 0 層，絕對外框——見 db/schedule.py。
    "start_booking", "end_booking",
)

# preparing＝尚未上架。玩家的清單只列上架中的，preparing 與 inactive
# 都看不到（見 db/listing.py）。上架時刻到了會自動轉成 active，
# 下架時刻到了會自動轉成 inactive。
SELECTABLE_STATUSES = ("preparing", "active", "inactive")


def _row_to_dict(row) -> dict:
    (mid, name, period, ready, reset, price, cost, players, wl, status, room_id,
     start_booking, end_booking,
     n1, u1, n2, u2, n3, u3, n4, u4) = row
    return {
        "id": mid,
        "name": name,
        # NUMERIC 在 psycopg 是 Decimal，JSON 序列化不了
        "period": float(period) if period is not None else None,
        # 佈置與還原時間。包廂的佔用區間是 [開始-ready, 開始+period+reset]，
        # 衝突判定比的是這個，不是 period 本身。
        "ready_time_cost": float(ready) if ready is not None else 0.0,
        "reset_time_cost": float(reset) if reset is not None else 0.0,
        "price": price,
        "booking_cost": cost,
        "players": players,
        "waitlist_limit": wl,
        "status": status,
        # 場次衝突比對的是包廂被佔用，不是同一齣戲被排兩場——見
        # db/bookings.py 的 has_room_conflict()。
        "room_id": room_id,
        # 上下架日期。None 代表那一側沒有限制；填一個很遠的真實日期
        # 效果完全相同，不要當成 magic value 特別處理。
        # datetime-local 吃的是 'YYYY-MM-DDTHH:MM'，多了秒數有些瀏覽器
        # 會整個不顯示。
        "start_booking": start_booking.strftime("%Y-%m-%dT%H:%M") if start_booking else None,
        "end_booking": end_booking.strftime("%Y-%m-%dT%H:%M") if end_booking else None,
        # 四個角色一律回傳，包含名稱為 NULL 的——管理員介面要能編輯
        # 「這個劇本原本沒有第 3 個角色，現在要加一個」，所以不能只回
        # 有名字的那幾個。名稱為 None 代表這個角色不存在，前端據此
        # 禁用對應的主持人名單區塊。
        "gm_slots": [
            {"slot": 1, "name": n1, "user_ids": list(u1 or [])},
            {"slot": 2, "name": n2, "user_ids": list(u2 or [])},
            {"slot": 3, "name": n3, "user_ids": list(u3 or [])},
            {"slot": 4, "name": n4, "user_ids": list(u4 or [])},
        ],
    }


_COLUMNS = (
    "id, name, period, ready_time_cost, reset_time_cost, "
    "price, booking_cost, players, waitlist_limit, status, room_id, "
    "start_booking, end_booking, "
    "gm1_name, gm1_user, gm2_name, gm2_user, gm3_name, gm3_user, gm4_name, gm4_user"
)


def list_all(cur) -> list[dict]:
    """全部劇本，含停用的——管理員要看得到才能重新啟用。"""
    cur.execute(f"SELECT {_COLUMNS} FROM mmg ORDER BY id")
    return [_row_to_dict(r) for r in cur.fetchall()]


def get(cur, mmg_id: int) -> dict | None:
    cur.execute(f"SELECT {_COLUMNS} FROM mmg WHERE id = %s", (mmg_id,))
    row = cur.fetchone()
    return _row_to_dict(row) if row else None


def list_rooms(cur) -> list[dict]:
    """所有包廂。給管理員編輯劇本時選用。"""
    cur.execute("SELECT id, name FROM rooms ORDER BY id")
    return [{"id": r[0], "name": r[1]} for r in cur.fetchall()]


def list_host_candidates(cur) -> list[dict]:
    """能被指派為主持人的使用者：role <= 2（主持人與管理員）且啟用中。

    停用的不列出來——不該讓管理員把一個已經停用的帳號指派成新場次的
    主持人。已經被指派的停用帳號仍會留在 gm{n}_user 陣列裡（那是歷史
    事實），只是不會出現在「新增」的選項中。
    """
    cur.execute(
        "SELECT id, name, email, role FROM users "
        "WHERE role <= 2 AND status = 'active' ORDER BY role, name"
    )
    return [{"id": r[0], "name": r[1], "email": r[2], "role": r[3]} for r in cur.fetchall()]


def _validate_slots(gm_slots: list[dict]) -> None:
    """檢查四個角色的名稱與名單是否自洽。

    這條規則資料庫也有 CHECK 約束擋（gm{n}_slot_consistent），但在這裡
    先擋一次是為了給得出人看得懂的訊息——直接讓資料庫拋
    CheckViolation，管理員只會看到一串英文約束名稱，不知道是哪個角色
    出問題。
    """
    if len(gm_slots) != 4:
        raise BookingError("gm_slots 需要剛好 4 個角色（沒有的角色 name 給 null）")
    for i, s in enumerate(gm_slots, start=1):
        name = (s.get("name") or "").strip() or None
        users = s.get("user_ids") or []
        if name is None and users:
            raise BookingError(f"角色 {i} 沒有名稱，不能指派主持人")
        if len(set(users)) != len(users):
            raise BookingError(f"角色 {i} 的主持人名單有重複")


def _validate_listing(start: str, end: str | None) -> None:
    """上架必須早於下架。

    ★ 一個結束早於開始的區間，代表它從來不曾開放過——那不是一種設定，
      是一筆壞資料。在這裡擋是為了回一句看得懂的話；資料庫的
      mmg_listing_order 約束是最後一道。
    """
    if not end:
        return
    if _parse_dt(end) <= _parse_dt(start):
        raise BookingError("下架時間必須晚於上架時間")


def _parse_dt(value: str) -> datetime:
    """接受 'YYYY-MM-DD' 與 'YYYY-MM-DDTHH:MM'（前端 datetime-local 的格式）。

    只有日期時補 00:00——那正是「那一天開始」的意思，跟舊資料的語意一致。
    """
    text = str(value).strip().replace(" ", "T")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise BookingError(f"時間格式錯誤「{value}」") from None


def _start_for_create(data: dict) -> str:
    """新增時的上架日期。

    ★ 「沒有這個欄位」與「這個欄位是空的」要分開處理，兩者是不同的意圖：
        沒帶  → 呼叫端根本沒管這件事，套用預設值＝今天（台北）。
                案主要的「新劇本預設為建置當天」就是這一條。
        空字串 → 使用者把它清掉了，那是明確的「我不填」，要拒絕。

      合成同一件事的話，前端表單清空後按下新增會靜靜地得到今天，
      而使用者以為自己成功地把它留白了。
    """
    if "start_booking" not in data:
        return _now_taipei()
    return _require_start(data)


def _require_start(data: dict) -> str:
    """取出上架日期，空的就拒絕。

    ★ 一齣連上架日期都掛不出來的劇本，代表它還沒有真的要賣，那它就不該
      出現在玩家可以預定的清單裡。允許留空的話，「先把資料建進去，哪天
      再說」的劇本會一直躺在那裡可以被訂走。
      資料庫那一層也是 NOT NULL，這裡先擋是為了回一句看得懂的話，而不是
      讓使用者收到一個 psycopg 的約束錯誤。
    """
    value = (data.get("start_booking") or "").strip()
    if not value:
        raise BookingError("上架時間不能空白")
    _parse_dt(value)   # 格式先驗，錯了要在這裡講，不是丟給 psycopg
    return value


def update(cur, mmg_id: int, data: dict) -> bool:
    """更新一齣劇本。回傳這次呼叫是否真的改到任何欄位。

    主持人名單跟其他欄位在同一次呼叫、同一個交易裡更新——前端的編輯
    畫面是「改完一起按儲存」，不是加一個人就送一次，所以這裡也不拆成
    兩支 API。
    """
    current = get(cur, mmg_id)
    if current is None:
        raise BookingError(f"劇本 id={mmg_id} 不存在")

    gm_slots = data.get("gm_slots")
    if gm_slots is None:
        raise BookingError("缺少 gm_slots")
    _validate_slots(gm_slots)

    if data.get("status") not in SELECTABLE_STATUSES:
        raise BookingError(f"未知的狀態「{data.get('status')}」")
    if not (data.get("name") or "").strip():
        raise BookingError("劇本名稱不能空白")

    names = [(s.get("name") or "").strip() or None for s in gm_slots]
    user_ids = [list(s.get("user_ids") or []) for s in gm_slots]

    new = {
        "name": data["name"].strip(),
        "period": data.get("period"),
        "ready_time_cost": data.get("ready_time_cost"),
        "reset_time_cost": data.get("reset_time_cost"),
        "price": data.get("price"),
        "booking_cost": data.get("booking_cost"),
        "players": data.get("players"),
        "waitlist_limit": data.get("waitlist_limit"),
        "status": data["status"],
        "room_id": data.get("room_id") or 1,
        # ★ 上架日期不可為空，見下方 _require_start 的說明。
        "start_booking": _require_start(data),
        # 下架日期可以留空 → NULL ＝ 沒有上限。日期輸入框清空時送過來的
        # 是 ""，直接塞進 DATE 欄位會噴型別錯誤，所以要轉成 None。
        "end_booking": (data.get("end_booking") or "").strip() or None,
    }

    _validate_listing(new["start_booking"], new["end_booking"])

    # 逐欄比對，但有兩種欄位不能直接比字面值：
    #
    #   period / ready / reset —— Decimal vs float，直接比會誤判成有變動。
    #
    #   start_booking / end_booking —— 同一個時刻有多種等價寫法。資料庫
    #     回來的是 '2999-12-31T00:00'，而呼叫端可能送 '2999-12-31'。
    #     ★ 比字串會讓這兩者永遠不相等：每次儲存都判定成有變動、下一次
    #       無謂的 UPDATE，而且永遠跳「已儲存」而不是「無任何修改」。
    #       兩邊都 parse 成 datetime 再比。
    def same(k):
        a, b = new[k], current[k]
        if k in ("period", "ready_time_cost", "reset_time_cost"):
            return float(a) == float(b) if a is not None and b is not None else a == b
        if k in ("start_booking", "end_booking"):
            if a is None or b is None:
                return a == b
            return _parse_dt(a) == _parse_dt(b)
        return a == b

    unchanged = all(same(k) for k in EDITABLE)
    slots_unchanged = all(
        names[i] == current["gm_slots"][i]["name"]
        and user_ids[i] == current["gm_slots"][i]["user_ids"]
        for i in range(4)
    )
    if unchanged and slots_unchanged:
        return False

    cur.execute(
        """
        UPDATE mmg SET
            name = %s, period = %s, ready_time_cost = %s, reset_time_cost = %s,
            price = %s, booking_cost = %s,
            players = %s, waitlist_limit = %s, status = %s, room_id = %s,
            start_booking = %s, end_booking = %s,
            gm1_name = %s, gm1_user = %s,
            gm2_name = %s, gm2_user = %s,
            gm3_name = %s, gm3_user = %s,
            gm4_name = %s, gm4_user = %s
         WHERE id = %s
        """,
        (
            new["name"], new["period"],
            new["ready_time_cost"], new["reset_time_cost"],
            new["price"], new["booking_cost"],
            new["players"], new["waitlist_limit"], new["status"], new["room_id"],
            new["start_booking"], new["end_booking"],
            names[0], user_ids[0],
            names[1], user_ids[1],
            names[2], user_ids[2],
            names[3], user_ids[3],
            mmg_id,
        ),
    )
    return True


def create(cur, data: dict) -> int:
    """新增一齣劇本，回傳新的 id。"""
    gm_slots = data.get("gm_slots") or [{"name": None, "user_ids": []} for _ in range(4)]
    _validate_slots(gm_slots)
    if not (data.get("name") or "").strip():
        raise BookingError("劇本名稱不能空白")

    names = [(s.get("name") or "").strip() or None for s in gm_slots]
    user_ids = [list(s.get("user_ids") or []) for s in gm_slots]

    start = _start_for_create(data)
    end = (data.get("end_booking") or "").strip() or None
    _validate_listing(start, end)

    cur.execute(
        """
        INSERT INTO mmg
            (name, period, ready_time_cost, reset_time_cost,
             price, booking_cost, players, waitlist_limit, status, room_id,
             start_booking, end_booking,
             gm1_name, gm1_user, gm2_name, gm2_user,
             gm3_name, gm3_user, gm4_name, gm4_user)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            data["name"].strip(), data.get("period"),
            data.get("ready_time_cost", 0.5), data.get("reset_time_cost", 0.5),
            data.get("price"),
            data.get("booking_cost"), data.get("players"),
            data.get("waitlist_limit", 3), data.get("status", "active"),
            data.get("room_id") or 1,
            start, end,
            names[0], user_ids[0],
            names[1], user_ids[1],
            names[2], user_ids[2],
            names[3], user_ids[3],
        ),
    )
    return cur.fetchone()[0]
