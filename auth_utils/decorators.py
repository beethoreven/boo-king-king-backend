"""
受保護路由的裝飾器。

★ 前端的鎖是化妝品，真正的門在這裡。任何人都能用 devtools 或直接打 API
   繞過前端的鎖，所以每一個需要授權的路由都必須掛上這裡的裝飾器——
   不能只靠前端把按鈕變灰。
"""

from __future__ import annotations

from functools import wraps

from flask import g, jsonify, request

from auth_utils import whitelist
from auth_utils.google_auth import extract_bearer_token
from auth_utils.sessions import resolve_session
from db.connection import pool


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
    if user["status"] != "active":
        return None, (jsonify({"error": "此帳號未獲授權", "code": "deactivated"}), 403)
    return user, None


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
