"""
應用程式自己簽發的登入 session。

跟 Google 完全解耦：Google 只在登入那一刻證明一次身分，之後所有請求都用
這裡發出的 token。設計理由見 db/schema.py 的 _ensure_sessions。
"""

from __future__ import annotations

import secrets

# 連續幾天完全沒有任何請求才讓 session 失效。這是「滑動窗口」不是固定期限——
# 每次成功查驗都會把計時歸零，所以只要使用者還在用，就永遠不會過期。
# 90 天是針對「久久才來訂一次遊戲」的玩家：隔兩個月回來還能直接用。
SESSION_IDLE_DAYS = 90

# last_seen_at 至少隔這麼久才寫一次。不節流的話，前端每次輪詢都會變成
# 一次資料庫寫入——讀取變成寫入是很容易忽略的成本放大。
_TOUCH_THROTTLE_SECONDS = 60


def create_session(cur, email: str) -> str:
    """簽發一張新的 session token。

    用 secrets.token_hex 而不是 JWT：前端不需要解析內容，只要原樣帶回來。
    不可解析反而是優點——沒有人會想從 token 裡讀權限，權限一律當場查
    資料庫（見 whitelist.py 的說明）。
    """
    token = secrets.token_hex(32)
    cur.execute(
        "INSERT INTO sessions (token, email) VALUES (%s, %s)",
        (token, email.lower().strip()),
    )
    return token


def resolve_session(cur, token: str) -> str | None:
    """把 token 換成 email。無效或閒置過久回傳 None。

    有效的話順便更新 last_seen_at（節流過），讓滑動窗口往前推。
    """
    if not token:
        return None

    cur.execute(
        """
        UPDATE sessions
           SET last_seen_at = NOW()
         WHERE token = %s
           AND last_seen_at > NOW() - make_interval(days => %s)
           AND last_seen_at < NOW() - make_interval(secs => %s)
        RETURNING email
        """,
        (token, SESSION_IDLE_DAYS, _TOUCH_THROTTLE_SECONDS),
    )
    row = cur.fetchone()
    if row is not None:
        return row[0]

    # 走到這裡有兩種可能：token 無效／過期，或是距離上次更新還不到節流間隔
    # （這種情況上面的 UPDATE 不會命中，但 session 其實是有效的）。
    # 用一次純查詢把兩者分開。
    cur.execute(
        """
        SELECT email FROM sessions
         WHERE token = %s AND last_seen_at > NOW() - make_interval(days => %s)
        """,
        (token, SESSION_IDLE_DAYS),
    )
    row = cur.fetchone()
    return row[0] if row else None


def delete_session(cur, token: str) -> None:
    """登出。找不到 token 也視為成功——呼叫端要的結果（這張 token 不再有效）
    本來就已經成立了，回報失敗只會讓前端多寫一段沒意義的錯誤處理。"""
    cur.execute("DELETE FROM sessions WHERE token = %s", (token,))


def delete_sessions_for_email(cur, email: str) -> int:
    """撤銷某個帳號的所有 session。停用帳號時一併呼叫，讓對方立刻登出，
    而不是等權限快取 TTL 過期。回傳撤銷了幾張。"""
    cur.execute("DELETE FROM sessions WHERE lower(email) = %s", (email.lower().strip(),))
    return cur.rowcount


def purge_expired(cur) -> int:
    """清掉已經過期的 session。不影響正確性（查驗時本來就會過濾），
    純粹是不要讓這張表無限長大。可以之後接排程，現在先留著手動用。"""
    cur.execute(
        "DELETE FROM sessions WHERE last_seen_at <= NOW() - make_interval(days => %s)",
        (SESSION_IDLE_DAYS,),
    )
    return cur.rowcount
