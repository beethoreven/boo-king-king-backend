"""
Google ID Token 的驗證。

★ 這裡的函式**只在登入那一刻被呼叫一次**（見 app.py 的 /auth/login），
   絕對不要在 require_auth 或任何受保護的路由裡呼叫。

原因：Google ID Token 大約一小時就過期，那是它的設計目的——它證明的是
「此刻這確實是一個 Google 認證過的身分」，不是「這個人接下來整段時間都有
權限」。若拿它當每次 API 呼叫的憑證，使用者會在登入約一小時後被無預警
登出，而所有手動測試都測不出來（測試流程通常幾分鐘就結束）。

正確做法是登入時用它換一張我們自己簽發的 session token（見
auth_utils/sessions.py），之後一律用那張。
"""

from __future__ import annotations

import os

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")


def extract_bearer_token(request) -> str | None:
    """從 Authorization: Bearer <token> 取出 token。"""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[len("Bearer "):].strip()
    return token or None


def verify_google_id_token(token: str) -> str | None:
    """驗證 Google ID Token，回傳已驗證的 email；失敗回傳 None。

    verify_oauth2_token 會一次檢查簽章、有效期限與 audience（必須是我們自己
    的 GOOGLE_CLIENT_ID——少了這項檢查，任何人拿其他 Google 應用簽發的
    token 都能通過）。

    另外要求 email_verified：Google 帳號可以綁一個未經驗證的 email，
    沒檢查這個等於接受任何人自稱的信箱。
    """
    if not token or not GOOGLE_CLIENT_ID:
        return None
    try:
        payload = google_id_token.verify_oauth2_token(
            token, google_requests.Request(), GOOGLE_CLIENT_ID
        )
    except Exception:  # noqa: BLE001 - 任何驗證失敗都一視同仁，不洩漏細節
        return None

    if not payload.get("email_verified"):
        return None
    email = payload.get("email")
    return email.lower().strip() if email else None
