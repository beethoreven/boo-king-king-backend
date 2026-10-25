"""
誰可以使用這個系統——以 users 資料表為準。

用資料庫而不是環境變數：環境變數要重新部署才會生效，資料表則是管理員在
後台改完就即時生效。這個專案本來就有 users 表（帶 status 與 role），
不需要再另外維護一份名單。

授權判斷是「這個 email 在 users 表裡，且 status = 'active'」。
停用帳號只要把 status 改掉，不用刪資料——這也是 mmg.gm{n}_user 敢用陣列
存 user id 的前提（帳號列永遠留著，id 永遠有效）。

★ 這個檢查必須「每次請求都查」，不能在登入時查一次就把結果寫進 session。
   兩者的差別在於撤銷權限多久生效：查一次的話，被停用的帳號會一直用到
   session 自然過期為止（可能是好幾十天）；每次查則是快取 TTL 之內就失效。
   這正是我們敢讓 session token 活很久的原因——安全性由「當下的權限狀態」
   保證，不是靠 token 本身早點過期。

★ 多店家：權限快取是每家店一份。同一個 email 在 A 店是管理員、在 B 店是
   一般玩家（或根本沒註冊）是正常的；快取全行程共用的話，B 店的請求會讀到
   A 店的角色與使用者編號。
"""

from __future__ import annotations

import os
import threading
import time

import tenancy

# 快取存活秒數。這個查詢會出現在每一個受保護的請求上，沒有快取的話
# 每次請求都要多一次資料庫往返。30 秒是「撤銷權限多快生效」與
# 「省下多少往返」之間的取捨——夠短到管理員停用帳號後幾乎立刻有感，
# 又夠長到把輪詢類的重複查詢吸收掉。
#
# 註（承自 zh-cn-to-tw 的教訓）：那邊當初訂 TTL 的量化理由是「單次連線+
# 查詢實測 1.2-1.6 秒」，但那個數字有 99% 來自 Render 與 Neon 跨區部署，
# 同區之後降到約 30ms。本專案從第一天就同區（都在 ap-southeast-1），
# 所以那個理由在這裡從來就不成立——快取留著是為了省 Neon 免費方案的
# 連線數與 CU 用量，不是為了省那 1.5 秒。要調 TTL 之前請先量現況。
_CACHE_TTL_SECONDS = int(os.environ.get("AUTH_CACHE_TTL_SECONDS", "30"))

# email -> (到期時間戳, 使用者資料 dict 或 None)，每家店一份
_cache = tenancy.PerTenant(dict)
_cache_lock = threading.Lock()


def _fetch_user(cur, email: str) -> dict | None:
    """讀一個帳號。★ status 回傳的是**當下生效的**狀態，不是資料庫裡存的字。

    停權是有期限的，而到期那一刻不會有人跑來把狀態改回去（那需要一個每分鐘
    醒來的排程，而那會讓 Neon 永遠不休眠——理由見 db/listing.py）。所以這裡
    直接在 SQL 裡算：banned 但時間已過就等於 active。

    資料庫裡那個字什麼時候才會被改回來？每週的排程順手做（guard.decay_points）。
    那只是為了讓管理員在使用者清單上看到對的東西，不影響任何判定。
    """
    cur.execute(
        """
        SELECT id, email, name, role,
               CASE
                   WHEN status = 'banned'
                        AND banned_until IS NOT NULL
                        AND banned_until <= NOW()
                   THEN 'active'
                   ELSE status
               END AS effective_status,
               banned_until,
               abuse_point
          FROM users WHERE lower(email) = %s
        """,
        (email.lower().strip(),),
    )
    row = cur.fetchone()
    if row is None:
        return None
    uid, mail, name, role, status, banned_until, points = row
    return {
        "id": uid, "email": mail, "name": name, "role": role, "status": status,
        # 停權到期時間要帶出來——被擋下來的人要看到「停到什麼時候」，
        # 只說「你被停權了」會讓他不知道該等還是該打電話。
        "banned_until": banned_until,
        "abuse_point": points,
    }


def get_user(cur, email: str, *, fresh: bool = False) -> dict | None:
    """查使用者資料，帶短期快取。查不到回傳 None。

    fresh=True 會跳過快取直接查資料庫，並順便把結果寫回快取。

    什麼時候該用 fresh：**會佔用稀缺資源的寫入路徑**。快取的代價是
    「帳號被停用後，最多 _CACHE_TTL_SECONDS 秒內仍會被視為有效」。
    對唯讀查詢來說那個窗口無所謂；但對「搶下一個場次名額」這種寫入，
    在窗口內成立的預約會真的佔住位子，而停用帳號並不會把它吐回來——
    要另外人工取消。

    這個形狀是 zh-cn-to-tw 回饋過來的：他們的 job 一旦啟動就跑到完，
    中途不再檢查授權，所以剛被停用的人在快取窗口內還能啟動一個
    燒好幾分鐘 LLM 額度的工作。我們沒有那種長流程（預約是單一短交易），
    但「窗口內取得的東西撤銷不回來」這件事是一樣的，而且我們的資源
    有庫存、會被搶。

    成本很低，值得用在預約寫入上：多一次索引查詢（同區約 4ms），
    而預約的頻率是平時每天個位數、尖峰每分鐘幾十筆。
    """
    key = (email or "").lower().strip()
    if not key:
        return None

    cache = _cache.get()
    now = time.monotonic()
    if not fresh:
        with _cache_lock:
            hit = cache.get(key)
            if hit and hit[0] > now:
                return hit[1]

    # 刻意在鎖外面查資料庫。鎖只保護那個 dict 的讀寫，不能包住網路操作——
    # 一旦把資料庫查詢放進鎖裡，任何一次連線變慢都會讓所有等著查權限的
    # 請求一起卡住，那正是 zh-cn-to-tw 踩過的那種全站凍結。
    user = _fetch_user(cur, key)

    with _cache_lock:
        cache[key] = (now + _CACHE_TTL_SECONDS, user)
    return user


# peek 用的哨兵（公開，require_auth 要用）。快取裡「查過、確認這個 email 沒註冊」存的就是 None，
# 跟「沒查過」是兩回事——用 None 當作「沒查過」會讓未註冊的人每次請求都
# 重新查一次資料庫，而那正是最該被擋下來的那種流量。
MISS = object()


def peek(email: str):
    """只查快取，不碰資料庫。沒查過就回 MISS，查過就回使用者資料或 None。

    給 require_auth 用：跟 sessions.resolve_cached 搭配，兩層都命中的請求
    完全不需要開連線。★ 不要拿這支做授權判斷以外的事——它不會續期，
    也不會在快取過期時自己去補。
    """
    key = (email or "").lower().strip()
    if not key:
        return MISS
    cache = _cache.get()
    now = time.monotonic()
    with _cache_lock:
        hit = cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
    return MISS


def is_permitted_user(cur, email: str, *, fresh: bool = False) -> bool:
    """這個 email 現在可以使用系統嗎。

    預約寫入路徑請傳 fresh=True，理由見 get_user。
    """
    user = get_user(cur, email, fresh=fresh)
    return bool(user and user["status"] == "active")


def invalidate(email: str | None = None) -> None:
    """清掉快取（目前這家店的）。改動使用者權限後呼叫，讓變更立即生效而不用等 TTL。"""
    cache = _cache.get()
    with _cache_lock:
        if email is None:
            cache.clear()
        else:
            cache.pop(email.lower().strip(), None)


def register_self(cur, email: str, data: dict) -> dict:
    """使用者自己填完聯絡資料、建立帳號。回傳新建的使用者。

    ★ email 一律取自呼叫端傳進來的「session 裡那個」，不是使用者送上來的
      欄位。前端的 email 欄位是唯讀的顯示用途，就算有人改了它、或直接打
      API 帶別的 email，也影響不到這裡——身分只由 Google 驗過的那一個決定。

    ★ 角色寫死 3（一般玩家）。開放讓呼叫端指定，等於讓任何人自己註冊成
      管理員。

    ★ 已經在名單裡就拒絕，不論狀態。這條是給停權用的：如果被停權的人
      可以重新註冊一次把自己變回 active，停權就完全沒有意義了。
    """
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("缺少 email")

    cur.execute("SELECT status FROM users WHERE lower(email) = %s", (email,))
    row = cur.fetchone()
    if row is not None:
        raise ValueError("這個帳號已經註冊過了")

    name = (data.get("name") or "").strip()
    line_id = (data.get("line_id") or "").strip()
    phone = (data.get("phone") or "").strip()
    missing = [
        label for label, v in (("稱呼", name), ("LINE ID", line_id), ("電話", phone))
        if not v
    ]
    if missing:
        raise ValueError("以下欄位不能空白：" + "、".join(missing))

    cur.execute(
        "INSERT INTO users (email, name, role, status, line_id, phone) "
        "VALUES (%s, %s, 3, 'active', %s, %s) "
        "RETURNING id, email, name, role, status",
        (email, name, line_id, phone),
    )
    uid, mail, nm, role, status = cur.fetchone()
    invalidate(email)
    return {"id": uid, "email": mail, "name": nm, "role": role, "status": status}
