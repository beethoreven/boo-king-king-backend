"""使用者自己的資料：讀取、修改、刪除帳號。

跟 admin_utils/users_admin.py 分開，因為兩者能做的事不同：管理員可以改
角色與狀態、可以改別人的；這裡只能改自己的聯絡資料，而且改不到權限。
共用一支函式再用參數區分「是不是管理員」，那個參數遲早會有人傳錯。
"""

from __future__ import annotations

import re

from auth_utils import whitelist
from db import slot_cache

# LINE ID 允許英數字與 . - _ ——官方允許的就是這些。只認英數字會把合法的
# ID 判成錯誤，使用者會卡在一個他改不掉的紅字上。
LINE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# 台灣手機：09 開頭、共 10 碼。
PHONE_RE = re.compile(r"^09\d{8}$")

# 這三個欄位缺任何一個，就算聯絡資料不完整——跟註冊時「缺一不可」同一條
# 規則。註冊要求三個都填、事後卻可以只留一個，那是兩套標準。
CONTACT_FIELDS = ("name", "line_id", "phone")


def validate_contact(data: dict) -> dict:
    """檢查格式，回傳 {欄位: 錯誤訊息}。全部合法就是空 dict。

    空字串一律視為「不填」，不是格式錯誤——使用者有權把聯絡資料清空，
    代價是訂不了場，那是另一層的事。
    """
    errors = {}
    line_id = (data.get("line_id") or "").strip()
    phone = (data.get("phone") or "").strip()

    if line_id and not LINE_ID_RE.match(line_id):
        errors["line_id"] = "只能使用英文、數字與 . - _"
    if phone and not PHONE_RE.match(phone):
        errors["phone"] = "請輸入 09 開頭的 10 碼手機號碼"
    return errors


def has_complete_contact(user: dict) -> bool:
    return all((user.get(f) or "").strip() for f in CONTACT_FIELDS)


def get(cur, user_id: int) -> dict | None:
    cur.execute(
        "SELECT id, email, name, role, status, line_id, phone FROM users WHERE id = %s",
        (user_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0], "email": row[1], "name": row[2], "role": row[3],
        "status": row[4], "line_id": row[5], "phone": row[6],
    }


def update(cur, user_id: int, data: dict) -> dict:
    """改自己的聯絡資料，回傳更新後的資料。

    ★ 只碰 name / line_id / phone。email 是登入身分、role 是權限、status
      是管理員的決定——這三個從這裡碰得到，就等於開了一條讓任何人自己
      升權、或自己解除停權的路。
    """
    errors = validate_contact(data)
    if errors:
        raise ValueError("格式不正確")

    def clean(key):
        return (data.get(key) or "").strip() or None

    cur.execute(
        "UPDATE users SET name = %s, line_id = %s, phone = %s WHERE id = %s",
        (clean("name"), clean("line_id"), clean("phone"), user_id),
    )
    user = get(cur, user_id)
    whitelist.invalidate(user["email"])
    return user


class HasActiveBookings(Exception):
    """還有已成立的場次，不能刪帳號。"""


def delete_self(cur, user_id: int) -> int:
    """刪除自己的帳號，回傳順帶取消了幾筆預約。

    ★ 不是真的 DELETE 那一列。booking_record.player_id 參照 users，硬刪
      會讓既有預約的外鍵斷掉；而且店家仍然需要知道那些場次存在過。
      做法是清掉個資、把 status 改成 deleted——那一列還在，但已經不是
      一個可以登入的帳號了。

    還有 booked 場次時拒絕：那是已經談定、店家可能已經備料的場次，不該
    由使用者單方面按一個鍵就讓它變成無主的。其餘還沒定案的
    （gm_confirm / gm_reviewed）在刪除時一併取消——人都走了，沒有理由讓
    它們繼續佔著容量。ended 與 cancelled 是終點，不動。
    """
    cur.execute(
        "SELECT COUNT(*) FROM booking_record WHERE player_id = %s AND status = 'booked'",
        (user_id,),
    )
    if cur.fetchone()[0] > 0:
        raise HasActiveBookings

    cur.execute(
        "UPDATE booking_record SET status = 'cancelled' "
        "WHERE player_id = %s AND status NOT IN ('booked', 'ended', 'cancelled')",
        (user_id,),
    )
    cancelled = cur.rowcount

    cur.execute("SELECT email FROM users WHERE id = %s", (user_id,))
    row = cur.fetchone()
    email = row[0] if row else None

    cur.execute(
        "UPDATE users SET name = NULL, line_id = NULL, phone = NULL, "
        "status = 'deleted' WHERE id = %s",
        (user_id,),
    )
    if email:
        whitelist.invalidate(email)
    # 上面那句 UPDATE 把這個人未成立的場次全取消了，等於某些時段空出位子。
    # 這條路同樣沒有走 db/bookings.py 的寫入函式（2026-09-13 補上）。
    slot_cache.clear()
    return cancelled
