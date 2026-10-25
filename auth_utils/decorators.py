"""
受保護路由的裝飾器。

★ 前端的鎖是化妝品，真正的門在這裡。任何人都能用 devtools 或直接打 API
   繞過前端的鎖，所以每一個需要授權的路由都必須掛上這裡的裝飾器——
   不能只靠前端把按鈕變灰。
"""

from __future__ import annotations

import time
from functools import wraps

from flask import g, jsonify, request

import tenancy
from auth_utils import guard, whitelist
from auth_utils.google_auth import extract_bearer_token
from auth_utils import sessions as sessions_mod
from auth_utils.sessions import resolve_cached, resolve_session
from db.connection import pool

import notifications
import notify_events


def _authenticate() -> tuple[dict | None, tuple | None]:
    """查驗憑證，回傳 (使用者資料, 錯誤回應)。兩者恰有一個是 None。

    ★ 連線的取得與歸還都在這個函式內完成，路由函式執行時這條連線已經
      還回池子了。這一點是刻意的、不能改：如果在持有連線的狀態下呼叫
      路由，而路由自己也要拿一條連線，那就是「巢狀持有」——在
      max_size=5 的池子下，5 個併發請求各持 1 條又各等第 2 條，會全部
      死鎖。開賣尖峰同時 30+ 人時這不是理論風險。
    """
    token = extract_bearer_token(request)
    if not token:
        return None, (jsonify({"error": "未提供登入憑證"}), 401)
    # ★ 偽造的 token（包括別家店的）在這裡就擋掉，不開連線——開連線本身就會
    #   把這家店的資料庫叫醒（見 auth_utils/sessions.py 的「token 的形狀與簽章」）。
    if not sessions_mod.is_authentic(token):
        return None, (jsonify({"error": "登入憑證無效或已過期"}), 401)

    # ★ 兩層快取都命中就完全不開連線。
    #
    #   這是整個查驗路徑上最重要的一件事：身分查驗跑在每一支受保護的路由上，
    #   而它原本固定要一條連線。db/connection.py 的同時連線上限是 5，開賣
    #   尖峰時那 5 個名額就是瓶頸——即使 /slot 的回應已經被快取，還是得為了
    #   「你是誰」開一條，等於快取只把 2 條減成 1 條。
    #
    #   兩層是分開的、TTL 也不同（session 3 秒、權限 30 秒），所以必須各自
    #   問一次；任一層沒命中就走下面的完整流程，跟以前一模一樣。
    #
    #   ★ 這裡快取的都是「你是誰、有沒有權限」，不是任何業務資料。劇本清單、
    #     場次狀態都不經過這一層。
    email = resolve_cached(token)
    user = whitelist.peek(email) if email else whitelist.MISS
    if email is None or user is whitelist.MISS:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                email = resolve_session(cur, token)
                if email is None:
                    return None, (jsonify({"error": "登入憑證無效或已過期"}), 401)

                # 權限每次都查（帶 30 秒快取），不從 session 讀取。
                # 這樣管理員停用帳號後，對方最慢 30 秒就會被擋下來，
                # 不用等 session token 自然過期。
                user = whitelist.get_user(cur, email)
                conn.commit()  # resolve_session 可能更新了 last_seen_at

    # ★ 兩種 403 要分得開。
    #
    #   還沒註冊——Google 驗過身分了，只是還沒在這個系統留下聯絡資料。
    #             前端該做的是把註冊表單給他，不是叫他去找店家。
    #   被停權——名單裡有他，但管理員關掉了。這才是真正的「未獲授權」，
    #           自己註冊一次也救不回來（見 /auth/register）。
    #
    # 兩者都回 403，但帶不同的 code；訊息文字會變，code 不會，前端據此分流。
    if user is None:
        return None, (jsonify({"error": "尚未完成註冊", "code": "unregistered"}), 403)
    if user["status"] == "deleted":
        # 自己刪掉的帳號。跟停權分開講：停權是店家的決定，刪除是他自己的，
        # 兩者要走的復原路徑不同（都得找店家，但說法不該一樣）。
        return None, (jsonify({"error": "此帳號已刪除", "code": "deleted"}), 403)
    if user["status"] == "banned":
        # 系統判定的停權，跟店家手動停用（deactivated）分開回報：前者會自己
        # 到期、而且有明確的到期時間可以告訴當事人，後者只能找店家。
        # 前端據此顯示不同的畫面，見 web 的 blocked 畫面。
        return None, (jsonify({
            "error": "因異常行為暫時停權",
            "code": "banned",
            # 到期時間給前端顯示。None 代表無限期（abuse_rule 的 ban_seconds
            # 設 0），那種只能等管理員手動解。
            "banned_until": user["banned_until"].isoformat() if user.get("banned_until") else None,
            "admin_name": admin_display_name(),
        }), 403)
    if user["status"] != "active":
        return None, (jsonify({"error": "此帳號未獲授權", "code": "deactivated"}), 403)

    # ★ 速率限制放在這裡，而不是更前面：它是「按帳號」算的，得先知道是誰。
    #   代價是多花了一次 session 查詢才擋下來，但那換到的是「一個人打爆
    #   自己的額度不會影響別人」——全站限制在開賣尖峰會把真客人一起擋掉。
    #
    #   管理員豁免。如果唯一的管理員被系統自己鎖住，就沒有人能解鎖了。
    if user["role"] > 1:
        retry_after = guard.check_rate(user["id"])
        if retry_after is not None:
            _log_incident(user["id"], user["email"], guard.KIND_RATE_LIMIT,
                          f"每分鐘超過 {guard.RATE_LIMIT_PER_MINUTE} 次")
            resp = jsonify({"error": "請求過於頻繁，請稍候再試", "code": "rate_limited"})
            resp.headers["Retry-After"] = str(retry_after)
            return None, (resp, 429)

    return user, None


# 停權畫面要顯示「本店管理員是誰」。這個值幾乎不變，但被停權的人可能一直
# 重打——不快取的話，每一次被擋下來都要多一次資料庫查詢，等於把停權畫面
# 變成一個放大器。
#
# ★ 每家店一份：「本店」的管理員。只存一份的話，B 店被停權的人會看到 A 店
#   管理員的名字。
_ADMIN_NAME_TTL_SECONDS = 600


class _AdminName:
    __slots__ = ("expires_at", "name")

    def __init__(self) -> None:
        self.expires_at = 0.0
        self.name: str | None = None


_admin_name = tenancy.PerTenant(_AdminName)


def admin_display_name() -> str | None:
    """role 1 的管理員叫什麼。查不到就回 None，讓前端自己決定怎麼講。"""
    cached = _admin_name.get()
    now = time.monotonic()
    if cached.expires_at > now:
        return cached.name
    name = None
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name FROM users WHERE role = 1 AND status = 'active' "
                    "ORDER BY id LIMIT 1"
                )
                row = cur.fetchone()
                name = row[0] if row else None
    except Exception:  # noqa: BLE001 - 顯示用的資訊，查不到不該讓請求失敗
        pass
    cached.expires_at, cached.name = now + _ADMIN_NAME_TTL_SECONDS, name
    return name


def _log_incident(user_id: int, email: str, kind: str, detail: str) -> None:
    """把異常寫進 abuse_log；如果這一筆讓點數到了門檻，順便把人踢下線。

    紀錄失敗不能讓請求跟著失敗——這是觀測用的旁支，把它變成主要流程的
    單點故障，等於為了記錄問題而製造問題。

    ★ record() 內部會在「開了新的一列」時加點並改狀態（見 guard.py）。
      這裡負責善後的兩件事，而且必須一起做：刪 session 把人踢下線，
      清權限快取讓他不能立刻重新登入。

    ★ 通知信在 commit **之後**才送出。順序反過來的話，一個最後被 rollback
      掉的記點會留下一封「某某被停權了」的信，而管理員去看會找不到那個人
      的紀錄——信收不回來，只能不要寄錯。
    """
    outbox = None
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                outcome = guard.record(cur, user_id, kind, request.path, detail)
                cur.execute("SELECT status FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
                if row and row[0] == "banned":
                    sessions_mod.delete_sessions_for_email(cur, email)
                    whitelist.invalidate(email)
                outbox = notify_events.on_abuse(cur, user_id, kind, outcome)
                conn.commit()
    except Exception:  # noqa: BLE001 - 觀測失敗不該影響主要流程
        return
    notifications.dispatch(outbox)


def require_auth(fn):
    """需要登入且帳號有效。"""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        user, error = _authenticate()
        if error:
            return error
        g.user = user
        return fn(*args, **kwargs)

    return wrapper


def require_role(minimum: int):
    """需要至少某個等級的權限。

    權限是階層，數字越小越高（1 管理員 ⊃ 2 主持人 ⊃ 3 一般玩家），
    所以比較用 `<=`。寫成 `==` 會把管理員擋在主持人的功能外面。

        @require_role(2)   # 主持人以上（含管理員）
        @require_role(1)   # 僅管理員
    """

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user, error = _authenticate()
            if error:
                return error
            if user["role"] > minimum:
                return jsonify({"error": "權限不足"}), 403
            g.user = user
            return fn(*args, **kwargs)

        return wrapper

    return decorator
