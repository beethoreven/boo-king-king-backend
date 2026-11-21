"""還不知道是誰（或不管是誰）就要擋的限流：依來源 IP。

每個帳號各自的限流在 guard.py，但它要先知道「是誰」才算得了，所以擋不到兩種情況：

  1. **登入、註冊、狀態查詢。** /auth/login 不需要任何憑證就打得到；/auth/register 與
     /auth/status 需要一張 session，但還沒註冊的帳號也拿得到（登入就發）。這三支每次
     都要驗 Google 憑證或查資料庫，/auth/login 還會在資料庫多寫一列 session。沒有
     上限的話，一張有效的 Google 憑證（效期約一小時）就能一直換新 session、一直開
     連線，把那家店同時可用的連線佔滿（見 db/connection.py 的每店上限）。
  2. **一個人開好幾個帳號。** 任何有 Google 帳號的人都能註冊，每個帳號各有每分鐘
     guard.RATE_LIMIT_PER_MINUTE 次。開十個帳號就是十倍。這裡給同一個 IP 一個總量
     上限，不管它用了幾個帳號。

## 數字怎麼訂的

正常使用離這些上限都很遠：每次打開或重新整理頁面打一次 /auth/status、登入一次
/auth/login、註冊一次 /auth/register；一個玩家在開賣時找位子，一分鐘一百多次 API
就算很多了（見 guard.RATE_LIMIT_PER_MINUTE 的說明）。上限要留給「好幾個人共用一個
IP」的情況：同一個家裡的 Wi-Fi，以及手機網路（電信業者讓很多用戶共用同一個對外
IPv4）。

★ 已登入的 API 總量**只限流、不封鎖**：同一個 IP 後面可能真的有好幾個人，封鎖
  十分鐘會把無辜的人一起關在外面。登入／註冊／狀態查詢是不用登入就打得到的入口，
  屢犯的照簡介頁的做法封鎖十分鐘（見 ip_limit.check）。
★ 每家店一份、每個 worker 一份（ip_limit.py 開頭）。
"""

from __future__ import annotations

from flask import jsonify

import ip_limit
import tenancy

# 每個 IP 每分鐘的上限。
LOGIN_PER_MINUTE = 20        # /auth/login
REGISTER_PER_MINUTE = 10     # /auth/register
STATUS_PER_MINUTE = 60       # /auth/status
# 已登入的 API 總量（所有帳號合計）。每個帳號自己的上限是 240（guard.py），
# 這裡是它的五倍：同一個 IP 後面最多容得下五個「一分鐘打滿 240 次」的人。
API_PER_MINUTE = 1200

_AUTH_LIMITS = {
    "login": LOGIN_PER_MINUTE,
    "register": REGISTER_PER_MINUTE,
    "status": STATUS_PER_MINUTE,
}

_auth = tenancy.PerTenant(ip_limit.Limiter)
_api = tenancy.PerTenant(ip_limit.Limiter)


def check_auth(request, kind: str):
    """登入／註冊／狀態查詢的限流。通過回 None，被擋回 (429 回應, 狀態碼)。"""
    ip, _chain = ip_limit.client_ip(request)
    return _reject(ip_limit.check(_auth.get(), ip, kind, _AUTH_LIMITS[kind]))


def check_api(request):
    """已登入的 API 的 IP 總量上限。通過回 None，被擋回 (429 回應, 狀態碼)。"""
    ip, _chain = ip_limit.client_ip(request)
    return _reject(ip_limit.check(_api.get(), ip, "api", API_PER_MINUTE, block=False))


def _reject(retry_after: int | None):
    if retry_after is None:
        return None
    resp = jsonify({"error": "請求過於頻繁，請稍候再試", "code": "rate_limited"})
    resp.headers["Retry-After"] = str(retry_after)
    return resp, 429


def reset() -> None:
    """全部店家都清掉。只給測試用。"""
    _auth.reset()
    _api.reset()
