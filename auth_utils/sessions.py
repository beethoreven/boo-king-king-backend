"""
應用程式自己簽發的登入 session——跟 Google ID Token 完全脫鉤。

背景（照抄 zh-cn-to-tw 的實戰結論）：Google ID Token 效期固定約 1 小時。
早期實作直接把它當成前端每支 API 都要帶的長效憑證，等於整個工作階段
每小時被打斷一次。那邊是長時間的 Stage 1／校對跑不完、算好的結果存不下來；
對預約系統來說，最糟的觸發時機是玩家填完資料、按下送出的那一刻。

設計：
- token 是隨機不透明字串（secrets.token_hex，不是 JWT），前端不需要、
  也不應該嘗試解析它。
- 「滑動式」有效期：每次成功查驗都把 last_seen_at 更新成現在，只要使用者
  還有在用（不管多久用一次）就不會過期；連續 SESSION_IDLE_DAYS 天完全沒有
  任何請求才真的過期。這讓「不要在使用中途登出使用者」成為結構上的保證。
- 撤銷完全不靠這一層：whitelist.py 在每支受保護的路由都會即時查（30 秒
  快取）。session 有效只代表「這確實是我們發出去、還沒過期的憑證」，
  不代表這個帳號現在還有權限。兩層各司其職。

跟 zh-cn-to-tw 的差異：那邊每個函式各自開連線，導致一個已登入請求光是
驗證身分就固定開兩條連線（resolve_session 一條、is_permitted_user 一條）。
這裡改成由呼叫端傳入 cursor，require_auth 用同一條連線做完兩件事。
"""

from __future__ import annotations

import os
import secrets
import threading
import time

# 連續幾天完全沒有任何請求才讓 session 失效。90 天是針對「久久才來訂一次
# 遊戲」的玩家：隔兩個月回來還能直接用。
SESSION_IDLE_DAYS = int(os.environ.get("SESSION_IDLE_DAYS", "90"))

# last_seen_at 沒必要每次請求都真的寫一次資料庫。
#
# 節流用行程內的字典而不是寫在 SQL 條件裡：寫在 SQL 的話，被節流的請求
# （也就是絕大多數請求）會先跑一個什麼都沒更新的 UPDATE、再跑一次 SELECT，
# 變成兩次往返；用記憶體判斷則是常見情況一次往返就結束。
#
# 多個 worker 各有自己的字典，頂多讓寫入頻率比設定值高一些，不影響正確性。
_TOUCH_MIN_INTERVAL_SECONDS = 60
_touch_lock = threading.Lock()
_last_touched: dict[str, float] = {}

# ── token → email 的解析快取 ────────────────────────────────
#
# 為什麼要有：查驗身分是**每一支受保護的路由**都會做的事，而它固定要開一條
# 資料庫連線。db/connection.py 的同時連線上限是 5，開賣尖峰時那 5 個名額
# 就是瓶頸——一個被快取擋下的 /slot 查詢原本還是要為了「你是誰」開一條，
# 等於快取只把 2 條減成 1 條。加上這一層，命中的請求才真的變成 0 條。
#
# ★ 這一層快取的是「這張 token 是誰的」，**不是任何業務資料**。劇本清單、
#   場次狀態都不經過這裡，所以它不會讓任何人看到過期的畫面。
#
# ★ 它也讓速率限制變便宜。guard.check_rate 是純記憶體的，但它跑在身分查驗
#   **之後**——沒有這層快取時，一個正在被限流的攻擊者每次請求仍然會開一條
#   連線才被擋下來，防禦比服務還貴。有了它，第二次之後的請求在記憶體裡就
#   被擋掉了。
#
# TTL 取短：撤銷（登出、停用）在這個窗口內仍然有效。3 秒比 whitelist 那層
# 既有的 30 秒權限窗口小一個數量級，所以它不會是撤銷延遲的瓶頸。
# 設 0 就是停用這層快取。
_RESOLVE_TTL_SECONDS = float(os.environ.get("SESSION_CACHE_TTL_SECONDS", "3"))
_resolved: dict[str, tuple[float, str]] = {}


def create_session(cur, email: str) -> str:
    """簽發一張新的 session token。

    呼叫端（/auth/login）負責先驗證 Google ID Token 並確認授權，
    這裡不重複做那兩件事。
    """
    token = secrets.token_hex(32)
    cur.execute(
        "INSERT INTO sessions (token, email) VALUES (%s, %s)",
        (token, email.lower().strip()),
    )
    return token


def resolve_cached(token: str | None) -> str | None:
    """只查快取，不碰資料庫。沒有或已過期就回 None。

    給 require_auth 用：先問這裡，命中就完全不必開連線。★ 不要用這支
    取代 resolve_session——它不會更新 last_seen_at，也不會刪掉過期的列。
    """
    if not token or _RESOLVE_TTL_SECONDS <= 0:
        return None
    now = time.monotonic()
    with _touch_lock:
        hit = _resolved.get(token)
        return hit[1] if hit is not None and hit[0] > now else None


def resolve_session(cur, token: str | None) -> str | None:
    """token 有效就回傳對應的 email，並視情況更新 last_seen_at；
    無效（不存在或閒置過久）回傳 None，過期的話順便把那筆刪掉。

    成功的結果會進解析快取（見上方 _RESOLVE_TTL_SECONDS）。失敗的**不會**
    ——無效 token 通常代表有人在亂試，快取那個結果只是幫他省事，而且會讓
    「剛登入的新 token」在窗口內被誤判成無效。
    """
    if not token:
        return None

    # 有效性判斷交給資料庫做（欄位是 TIMESTAMPTZ），不要撈回 Python 再比對——
    # 那樣得處理字串解析與時區，而且會把「現在幾點」這件事分散到兩個地方。
    cur.execute(
        """
        SELECT email, last_seen_at > NOW() - make_interval(days => %s) AS fresh
          FROM sessions WHERE token = %s
        """,
        (SESSION_IDLE_DAYS, token),
    )
    row = cur.fetchone()
    if row is None:
        return None

    email, fresh = row
    if not fresh:
        cur.execute("DELETE FROM sessions WHERE token = %s", (token,))
        _forget(token)
        return None

    _maybe_touch(cur, token)
    if _RESOLVE_TTL_SECONDS > 0:
        with _touch_lock:
            _resolved[token] = (time.monotonic() + _RESOLVE_TTL_SECONDS, email)
    return email


def _maybe_touch(cur, token: str) -> None:
    now = time.monotonic()
    with _touch_lock:
        if now - _last_touched.get(token, 0.0) < _TOUCH_MIN_INTERVAL_SECONDS:
            return
        _last_touched[token] = now
    cur.execute("UPDATE sessions SET last_seen_at = NOW() WHERE token = %s", (token,))


def _forget(token: str) -> None:
    """忘掉這張 token 的所有行程內狀態。登出與過期都會走到這裡。"""
    with _touch_lock:
        _last_touched.pop(token, None)
        _resolved.pop(token, None)


def delete_session(cur, token: str | None) -> None:
    """登出。找不到 token 也視為成功——呼叫端要的結果（這張憑證不再有效）
    本來就已經成立，回報失敗只會讓前端多寫一段沒意義的錯誤處理。"""
    if not token:
        return
    cur.execute("DELETE FROM sessions WHERE token = %s", (token,))
    _forget(token)


def delete_sessions_for_email(cur, email: str) -> int:
    """撤銷某個帳號的所有 session。停用帳號時一併呼叫，讓對方立刻登出，
    而不是等權限快取 TTL 過期。回傳撤銷了幾張。"""
    target = email.lower().strip()
    cur.execute("DELETE FROM sessions WHERE lower(email) = %s", (target,))
    # 解析快取是 token → email，所以要掃過去找出屬於這個帳號的每一張。
    # 快取很小（只有近幾秒活躍過的 token），掃描的成本可以忽略；漏掉的
    # 代價則是被停用的人在 TTL 內還能通過查驗。
    with _touch_lock:
        for tok in [t for t, (_exp, mail) in _resolved.items() if mail == target]:
            _resolved.pop(tok, None)
            _last_touched.pop(tok, None)
    return cur.rowcount


def purge_expired(cur) -> int:
    """清掉已經過期的 session。不影響正確性（查驗時本來就會過濾並順手刪除），
    純粹是不要讓這張表無限長大。"""
    cur.execute(
        "DELETE FROM sessions WHERE last_seen_at <= NOW() - make_interval(days => %s)",
        (SESSION_IDLE_DAYS,),
    )
    return cur.rowcount
