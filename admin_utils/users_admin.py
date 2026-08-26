"""
管理員介面「使用者管理」的資料存取層。

「有沒有真的改到東西」由這一層判斷（同 mmg_admin 的說明）。
"""

from __future__ import annotations

from auth_utils import sessions, whitelist
from db.bookings import BookingError

EDITABLE = ("email", "name", "role", "status", "line_id", "phone")
SELECTABLE_STATUSES = ("active", "deactive")
ROLE_LABEL = {1: "管理員", 2: "主持人", 3: "一般玩家"}


def _row_to_dict(row) -> dict:
    return {
        "id": row[0], "email": row[1], "name": row[2],
        "role": row[3], "role_label": ROLE_LABEL.get(row[3], str(row[3])),
        "status": row[4], "line_id": row[5], "phone": row[6],
    }


_COLUMNS = "id, email, name, role, status, line_id, phone"


def list_all(cur) -> list[dict]:
    cur.execute(f"SELECT {_COLUMNS} FROM users ORDER BY id")
    return [_row_to_dict(r) for r in cur.fetchall()]


def get(cur, user_id: int) -> dict | None:
    cur.execute(f"SELECT {_COLUMNS} FROM users WHERE id = %s", (user_id,))
    row = cur.fetchone()
    return _row_to_dict(row) if row else None


def update(cur, user_id: int, data: dict) -> bool:
    """更新一個使用者。回傳這次呼叫是否真的改到任何欄位。

    email 是登入身分：Google 登入回傳的 email 拿來比對 users.email，
    改掉之後那個人就登不進來，而且他自己無法修復（要再找管理員改回去）。
    後端這裡不擋——管理員有正當理由要改（例如對方換了 Google 帳號）——
    但前端必須在欄位下方常駐提示，並在儲存時跳對話框二次確認。
    """
    current = get(cur, user_id)
    if current is None:
        raise BookingError(f"使用者 id={user_id} 不存在")

    email = (data.get("email") or "").strip().lower()
    if not email:
        raise BookingError("email 不能空白")
    if data.get("role") not in (1, 2, 3):
        raise BookingError(f"未知的角色「{data.get('role')}」")
    if data.get("status") not in SELECTABLE_STATUSES:
        raise BookingError(f"未知的狀態「{data.get('status')}」")

    new = {
        "email": email,
        "name": (data.get("name") or "").strip() or None,
        "role": data["role"],
        "status": data["status"],
        "line_id": (data.get("line_id") or "").strip() or None,
        "phone": (data.get("phone") or "").strip() or None,
    }
    if all(new[k] == current[k] for k in EDITABLE):
        return False

    cur.execute(
        "UPDATE users SET email = %s, name = %s, role = %s, status = %s, "
        "line_id = %s, phone = %s WHERE id = %s",
        (new["email"], new["name"], new["role"], new["status"],
         new["line_id"], new["phone"], user_id),
    )

    # 帳號從啟用轉成停用時，主動撤銷它名下所有 session。
    #
    # 這是 zh-cn-to-tw 實作同一個功能時回報過來的教訓：光刪 session
    # 是無效的，對方可以立刻重新登入。/auth/login 走的是同一支有 30 秒
    # 快取的權限查詢，命中時仍然回「active」，於是又拿到新 session、
    # 繞回原點。兩件事一定要一起做：
    #   1. 刪 session —— 把人踢下線
    #   2. 清授權快取 —— 否則他立刻就能重新登入
    #
    # （booking-king 的 /auth/login 另外用 fresh=True 跳過快取，本身
    # 已經擋得住這條路；這裡清快取是第二層保險，也讓其他讀快取的
    # 路徑立即反映停用。）
    #
    # 只在「active -> deactive」這個轉換上做，不是「只要 status 是
    # deactive 就做」——後者會讓管理員每次編輯一個早就停用的帳號
    # （改個名字之類）都重跑一次無意義的 DELETE。也不對所有 update
    # 做：改名字或調角色不該把人踢下線。
    if current["status"] == "active" and new["status"] == "deactive":
        sessions.delete_sessions_for_email(cur, new["email"])
        # 舊 email 跟新 email 都要清：快取是用 email 當鍵，這次呼叫
        # 如果同時改了 email，只清一個會漏掉另一個。
        whitelist.invalidate(new["email"])
        whitelist.invalidate(current["email"])
    elif new["email"] != current["email"] or new["role"] != current["role"]:
        # 改了 email 或角色，快取裡的舊資料要作廢，否則最多 30 秒內
        # 權限判斷還是用舊值。
        whitelist.invalidate(new["email"])
        whitelist.invalidate(current["email"])

    return True


def create(cur, data: dict) -> int:
    """新增使用者，回傳新 id。

    這個系統的帳號由管理員建立，不是自助註冊（/auth/login 找不到對應
    email 就直接回 403，不會自動建帳號）。
    """
    email = (data.get("email") or "").strip().lower()
    if not email:
        raise BookingError("email 不能空白")
    if data.get("role") not in (1, 2, 3):
        raise BookingError(f"未知的角色「{data.get('role')}」")

    cur.execute("SELECT 1 FROM users WHERE lower(email) = %s", (email,))
    if cur.fetchone():
        raise BookingError(f"email {email} 已經存在")

    cur.execute(
        "INSERT INTO users (email, name, role, status, line_id, phone) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (
            email,
            (data.get("name") or "").strip() or None,
            data["role"],
            data.get("status", "active"),
            (data.get("line_id") or "").strip() or None,
            (data.get("phone") or "").strip() or None,
        ),
    )
    return cur.fetchone()[0]
