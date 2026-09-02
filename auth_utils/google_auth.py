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

import base64
import json
import os
import re

import cachecontrol
import requests as requests_lib
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")

# 在模組層建立一次就好。這個物件內部包著一個 HTTP session，重複使用可以
# 沿用已經建立的連線去取 Google 的公開金鑰；每次驗證都 new 一個等於每次
# 重新握手。
#
# ★ 這裡原本寫著「google-auth 會自己處理金鑰的取得與輪替，不用我們管」。
#   **那是錯的。** 讀過套件原始碼（2.32.0 與最新的 2.50.0 完全一樣）：
#
#       def _fetch_certs(request, certs_url):
#           response = request(certs_url, method="GET")   # 無條件，沒有快取
#
#   verify_token() 每次都呼叫它，所以**每一次登入驗證都會對 Google 發一次
#   即時 HTTPS 請求**——不管 token 是真是假。實測 10 次驗證 = 10 次對外請求。
#
#   後果有兩個。一是真的登入每次都要多等一個對外往返；二是更嚴重的：
#   /auth/login 不需要任何憑證就打得到，而 gunicorn 只有 2 workers × 4 threads
#   = 8 個並行槽，每一發假 token 都佔住一個槽做一次阻塞的網路 I/O。
#   一台機器就能把服務打到沒有空槽。
#
#   解法是套件文件自己建議的（見 google/oauth2/id_token.py 的模組說明）：
#   用 CacheControl 包住 session，讓它遵守 HTTP 快取語意。
#
# ★ 為什麼用 CacheControl 而不是自己寫一個 TTL 快取：自己寫就得挑一個秒數，
#   挑錯的後果是「Google 輪替金鑰之後，登入壞掉一陣子然後自己好」——情境式、
#   有時效性、上線前測不出來，正是最難查的那種壞法。CacheControl 遵守
#   Google 自己回的標頭（實測 `public, max-age=20955, must-revalidate`），
#   過期後是重新驗證而不是拿舊的用，所以沒有那個時間窗。
#   而且萬一它沒接上，最壞情況只是「沒有快取」＝回到現在的行為。
_google_request = google_requests.Request(
    session=cachecontrol.CacheControl(requests_lib.session()),
)

# JWT 的形狀：三段 base64url，用點分隔。
#
# ★ 這個檢查**不會改變任何一次驗證的結果**：形狀不對的東西本來就過不了簽章
#   驗證。它換到的是「過不了的那些，在發出對外請求之前就被擋掉」——實測
#   亂寫的字串原本要 12ms 加一次對外請求，現在是 0.5 微秒、零請求。
#
# ★ 刻意寫得比 google-auth 自己的要求更寬鬆（允許 = 補位，長度上限給到 8192），
#   因為這裡寧可放過假的，也不能擋掉真的。而且它是純字串比對，對同一個 token
#   永遠給同樣的答案——要是我寫錯了，是**所有人都登不進來**，第一次測試就會
#   發現，不會變成偶爾才壞的那種。
_JWT_SHAPE = re.compile(r"^[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+$")
_MAX_ID_TOKEN_LENGTH = 8192


def _has_kid(token: str) -> bool:
    """header 裡有沒有 kid（用哪一把金鑰簽的）。

    ★ 為什麼要多這一道：google-auth 在 header 沒有 kid 時，會把 Google 目前
      公開的**每一把**金鑰都拿來試一次簽章。那是純 CPU，而且是未登入就打得到
      的路徑。實測（Google 目前公開 2 把）：

          沒有 kid（逼它每把都試）  25.24 ms   ← 攻擊者最划算的寫法
          kid 對不到任何一把         1.64 ms
          kid 是真的、簽章是假的      7.68 ms

      25ms 在 Render 的 0.1 CPU 上大約是 250ms，而 gunicorn 只有
      2 workers × 4 threads = 8 個並行槽——每秒約 32 次就能把服務佔滿。

    ★ 真的 Google ID Token 一定帶 kid：Google 同時公開多把金鑰，不指定 kid
      的話驗證端根本不知道要用哪一把。已用一張真的 token 對照過（2026-09-02），
      header 是 {"alg":"RS256","kid":"f10f…c271","typ":"JWT"}，而那個 kid
      確實在 Google 當下公開的兩把裡。

    ★ 這裡只檢查「有沒有」，**不檢查它在不在目前的金鑰清單裡**——那會多一個
      網路相依，而且會跟金鑰輪替搶時間，變成偶發性的登入失敗。
      要是這個假設哪天錯了，後果是**所有人同時登不進來**，一測就知道，
      而不是那種只有某些人、某些時候才壞的。
    """
    head = token.split(".", 1)[0]
    try:
        # base64url 不補位，這裡補回來再解
        raw = base64.urlsafe_b64decode(head + "=" * (-len(head) % 4))
        return bool(json.loads(raw).get("kid"))
    except Exception:  # noqa: BLE001 - 解不開就是形狀不對，跟沒有 kid 同義
        return False


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
    # 形狀不對的連 Google 都不用問（見上方 _JWT_SHAPE）。
    if len(token) > _MAX_ID_TOKEN_LENGTH or not _JWT_SHAPE.match(token):
        return None
    if not _has_kid(token):
        return None
    try:
        payload = google_id_token.verify_oauth2_token(
            token, _google_request, GOOGLE_CLIENT_ID
        )
    except Exception:  # noqa: BLE001
        # 刻意不只攔 ValueError（google-auth 對格式錯誤／簽章不符／過期／
        # audience 不對都是丟這個）：取金鑰那段是真的網路請求，連不上時
        # 丟的是別的型別，只攔 ValueError 會讓它一路變成 500。
        # 對呼叫端而言兩者都是「這張憑證現在不能用」，回同樣的結果即可。
        return None

    if not payload.get("email_verified"):
        return None
    email = payload.get("email")
    return email.lower().strip() if email else None
