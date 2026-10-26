"""
boo-king-king 後端。

健康檢查、保活端點、Google 登入與 session 層，以及劇本查詢與預約路由。

一個服務可以同時服務好幾家店：每個請求依網址前綴 /v/<店家> 決定是哪一家
（見 _select_tenant），之後所有東西——資料庫、快取、設定——都是那一家的。
隔離怎麼做、哪裡會漏，寫在 tenancy.py 的開頭。
"""

from __future__ import annotations

import hmac
import os
from datetime import date, time
from functools import wraps

from dotenv import load_dotenv

# 一定要在 import 任何會讀 os.environ 的模組之前載入。tenancy 在 import 當下
# 就會讀店家設定（VARIANT / TENANTS 與各店的 DATABASE_URL），晚一步就起不來。
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from flask import Flask, g, jsonify, request, url_for  # noqa: E402
from flask_cors import CORS  # noqa: E402

import tenancy  # noqa: E402

from auth_utils import guard, profile, sessions, whitelist  # noqa: E402
from auth_utils.decorators import admin_display_name, require_auth, require_role  # noqa: E402
from auth_utils.google_auth import extract_bearer_token, verify_google_id_token  # noqa: E402
from admin_utils import bookings_admin, cover_image, mmg_admin, users_admin  # noqa: E402
import jobs  # noqa: E402
from db import bookings as bookings_db
from db import busy as busy_db  # noqa: E402
from db import idle_gap  # noqa: E402
from db import intro as intro_db  # noqa: E402
from db import mmg as mmg_db  # noqa: E402
from db import listing  # noqa: E402
from db import roles  # noqa: E402
from db import schedule  # noqa: E402
from db import slot_cache  # noqa: E402
from db.bookings import BookingError  # noqa: E402
from db.connection import pool  # noqa: E402
from line_utils import client as line_client  # noqa: E402
from line_utils import handler as line_handler  # noqa: E402
from line_utils import images as line_images  # noqa: E402
from line_utils import signature as line_signature  # noqa: E402
import notifications  # noqa: E402
import notify_events  # noqa: E402
import intro_public  # noqa: E402

# ── log：每一行都標上店家 ────────────────────────────────────────
#
# 好幾家店共用一份 log，出事時要分得出是哪一家。掛在根 logger 上，所以
# 各模組的 log、Flask 的 app.logger、psycopg 的警告都會經過這裡。
#
# ★ 要在建立 Flask app **之前**掛：Flask 第一次用 app.logger 時，如果整條
#   logger 鏈上找不到 handler，會自己加一個（不帶店家標記的）。
# ★ 自己掛一個 handler 而不是用 basicConfig：basicConfig 在已經有 handler
#   時是空操作，而 gunicorn 會先設定好自己的 logging——那會讓這一行看起來
#   有生效、實際上什麼都沒做。
# ★ WARNING 就夠了。INFO 會讓每一次連線都印一行，Render 免費方案的 log
#   有保存上限，真正的錯誤會被洗掉。
#
# （這裡原本只把 psycopg_pool 的 logger 接出來。連線池已經在 2026-08-31 拿掉
#   了——見 db/connection.py——那一段一直留著沒有作用，2026-10-25 換成這個。）
import logging  # noqa: E402
import sys as _sys  # noqa: E402

_root_log = logging.getLogger()
if not any(isinstance(f, tenancy.LogContext) for h in _root_log.handlers for f in h.filters):
    _h = logging.StreamHandler(_sys.stderr)
    _h.setFormatter(logging.Formatter("[%(tenant)s] %(levelname)s %(name)s: %(message)s"))
    _h.addFilter(tenancy.LogContext())
    _root_log.addHandler(_h)
    _root_log.setLevel(logging.WARNING)

app = Flask(__name__)

# 請求本體的上限。原本沒設，而 Flask/Werkzeug 沒有預設值——也就是說
# /auth/login（未登入就打得到）可以收到任意大的 body，而且會被完整讀進
# 記憶體之後才輪到任何檢查。送一個超大的 body 比送假 token 還便宜。
#
# 64KB 遠大於這裡任何一個正常請求（最大的是場次編輯，數 KB），
# 而 Google ID Token 本身大約 1KB。超過就回 413，不進到路由。
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

# max_age 是預檢（OPTIONS）結果的快取秒數，不設的話每一次 API 呼叫都會
# 變成兩個 HTTP 請求。
#
# 為什麼會有預檢：前端與後端不同源（本機是 :5173 對 :5001，線上是靜態
# 託管對 Render），而我們每個請求都帶 Authorization 標頭——帶自訂標頭的
# 跨來源請求屬於「非簡單請求」，瀏覽器規定要先送 OPTIONS 問過才能送真正
# 的請求。這是瀏覽器的行為，後端擋不掉，只能讓它少問幾次。
#
# 影響的是開賣尖峰：三十個人同時搶同一個場次，不快取就是兩倍的請求量，
# 而 Render 免費方案的執行時間有限。
#
# ★ 快取是按「完整網址」分開存的，查詢字串算在裡面（實測：同一個網址連打
#   五次只送一次 OPTIONS；同一支 API 換三個日期就送三次）。所以省下來的
#   是「重複打同一個網址」那部分：
#     有效——/api/mmg、/api/gm/dashboard、POST /api/bookings 這些沒有
#            查詢字串的，以及開賣時大家不斷重整同一個場次
#     無效——瀏覽不同日期的場次、換篩選條件翻頁，每組參數都要各自預檢
#   別以為裝了這行就沒有預檢了。
#
# 瀏覽器對這個值有自己的上限（Chrome 2 小時、Safari 更短），設得再大也
# 會被夾到上限，所以填 1 小時，不是愈大愈好。
#
# 來源限制：每家店的 ALLOWED_ORIGINS 是逗號分隔的完整來源（含 scheme，不要
# 結尾斜線），例如 https://eli.boo-king-king.workers.dev
#
# Flask-CORS 只能設一份全站名單，所以這裡給它**所有店家來源的聯集**；
# 「這個來源只能打它自己那家店」由下面的 _select_tenant 在請求一開始擋。
# 少了那一道，eli 的前端就能用瀏覽器裡的登入憑證去打 tutor-arthur 的 API。
#
# ★ 單店寫法沒設定時放行全部，而且會在啟動時印一行警告。這是刻意的取捨：
#   設成「沒設定就全擋」的話，本機開發會在毫無提示的情況下整個壞掉，而
#   症狀是瀏覽器主控台的 CORS 錯誤——那是最難從畫面上看出原因的一種失敗。
#   多店家寫法則是必填，沒設起不來（理由見 tenancy._build）。
_all_origins = sorted({o for t in tenancy.all_tenants() for o in t.allowed_origins})
if all(t.allowed_origins for t in tenancy.all_tenants()):
    CORS(app, origins=_all_origins, max_age=3600)
else:
    print(
        "[warn] ALLOWED_ORIGINS 未設定，目前放行任何來源。正式環境請務必設定。",
        flush=True,
    )
    CORS(app, max_age=3600)

# ── 店家識別（/v/<店家> 前綴）──────────────────────────────────
#
# 網址形狀是 /v/<店家>/api/...，前綴放在**所有路徑最前面**。理由是這個
# 後端有 /api/... 與 /auth/... 兩種前綴，而前端的 API_BASE 只是主機位址——
# 前綴放最前面，前端只要改 index.html 一行，四十幾個呼叫點零改動；放中間
# （/api/<店家>/...）則要改每一個呼叫點，而且 /auth/... 那幾支塞不進去。
#
# ★ 這一段**決定這個請求連哪一家店的資料庫**（2026-10-25 起，一個服務
#   服務好幾家店）。原本的設計是「每店一個服務、這一段只當斷言」，那樣的
#   隔離是結構性的；現在改成路由決定，所以防線換成兩道：
#     1. 登入憑證存在各店自己的資料庫——選錯店，憑證就查不到（401）。
#     2. 行程內的快取一律按店家分開——tenancy.PerTenant，由測試強制。
#   細節見 tenancy.py 開頭。
#
# ★ 店家清單沒設好就起不來（tenancy.load 會拋），不給預設值。Render 部署
#   失敗會保留舊版本，現有服務不會中斷。


class _StripVariant:
    """把 /v/<variant> 從路徑上剝掉，讓後面四十幾支路由完全不用改。

    只負責剝除與記錄，**不做判斷**——判斷放在下面的 before_request。
    在 WSGI 這一層直接回 404 的話，那個回應不會經過 Flask-CORS，瀏覽器
    只會看到一個沒有 CORS 標頭的失敗，主控台顯示成跨來源錯誤——把「打錯
    服務」偽裝成「CORS 設定壞了」，是最難從畫面判斷原因的一種。
    """

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path.startswith("/v/"):
            # ['', 'v', '<variant>', '剩下的路徑']
            parts = path.split("/", 3)
            variant = parts[2] if len(parts) > 2 else ""
            environ["boo_king_king.variant"] = variant
            environ["PATH_INFO"] = "/" + (parts[3] if len(parts) > 3 else "")
            # SCRIPT_NAME 要一起補上，否則 url_for 產生的網址會少掉前綴。
            environ["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "") + f"/v/{variant}"
        return self.wsgi_app(environ, start_response)


app.wsgi_app = _StripVariant(app.wsgi_app)


# 唯一不需要前綴的路徑，而且是**永久**的例外，不是過渡措施。
#
# /api/health 刻意不碰資料庫、不回任何店家資料——沒有一個欄位是店家能區分
# 的，所以它不需要知道是哪一家店。
#
# ★ 真正的理由是它的呼叫者不歸我們管：Render 自己的健康檢查、外部的監控
#   服務都可能打這一支，而那些地方不會帶前綴。把它一起擋掉的話，故障會是
#   「服務被判定為不健康而反覆重啟」——跟店家隔離完全無關，卻很難聯想到
#   是這個檢查造成的。
_NO_PREFIX_OK = {"/api/health"}


@app.before_request
def _select_tenant():
    """依前綴決定這個請求是哪一家店；之後這個請求碰到的一切都是那一家的。

    三種拒絕，都說清楚網址該長什麼樣（設定錯誤要看得見）：
      - **沒帶前綴**（多半是前端或排程的網址漏改）：404
      - **前綴不是這個服務的任何一家店**：404
      - **來源（Origin）不是這家店的前端**：403，見下面

    ★ 錯誤訊息**不列出**這個服務有哪些店。路過的人亂猜前綴，不該拿到客戶名單。

    ★ 每個請求一開始都會重設目前的店家（沒帶前綴就設成「沒有」）。gunicorn
      的執行緒會被重複使用，不重設的話，上一個請求的店家會留給下一個。
    """
    got = request.environ.get("boo_king_king.variant")
    tenant = tenancy.get(got) if got is not None else None
    g.tenant_token = tenancy.activate(tenant)

    if got is None:
        if request.path in _NO_PREFIX_OK:
            return None
        return jsonify({
            "error": "缺少店家識別前綴",
            "detail": f"網址要以 /v/<店家識別>/ 開頭，例如 /v/<店家識別>{request.path}",
        }), 404

    if tenant is None:
        return jsonify({
            "error": "找不到這家店",
            "detail": f"網址前綴 /v/{got or '(空的)'}/ 不是這個服務的店家。請檢查前端的 API 網址。",
        }), 404

    # ★ 來源檢查。CORS 名單是所有店家的聯集（見上方），所以「eli 的前端打
    #   tutor-arthur 的 API」在瀏覽器那一關會被放行——要在這裡擋。
    #   只看有帶 Origin 的請求：瀏覽器的跨來源請求一定會帶；排程、LINE 的
    #   webhook、LINE App 來抓圖都不帶，它們本來就不受 CORS 管。
    #   這一道防的是「別家店的網頁替使用者打這家店的 API」，不是防直接打 API
    #   的人——那種人本來就要有這家店的憑證才做得了任何事。
    origin = request.headers.get("Origin")
    if origin and tenant.allowed_origins and origin.rstrip("/") not in tenant.allowed_origins:
        return jsonify({
            "error": "這個網站不能呼叫這家店的 API",
            "detail": f"來源 {origin} 不在 {tenant.id} 的 ALLOWED_ORIGINS 裡。",
        }), 403

    return None


@app.teardown_request
def _release_tenant(_exc):
    """請求結束，把目前的店家還原（見 _select_tenant 的說明）。"""
    token = g.pop("tenant_token", None)
    if token is not None:
        tenancy.deactivate(token)


for _t in tenancy.all_tenants():
    print(
        f"[info] 店家 {_t.id}：接受 /v/{_t.id}/... ｜"
        f"Google client {'已設定' if _t.google_client_id else '未設定（登入會失敗）'} ｜"
        f"排程 token {'已設定' if _t.job_token else '未設定（排程端點停用）'} ｜"
        f"LINE {'啟用' if _t.line_enabled else '停用'}",
        flush=True,
    )

# 啟動時說一聲通知信是開還是關。
#
# ★ 這一行存在的理由是「沉默的失敗」：BREVO_API_KEY 沒設或打錯字時，整個
#   通知功能會安靜地停用——不會有任何錯誤、任何請求都照常成功，只是信永遠
#   不會寄出去，而寄不出去這件事沒有人會回報。沒有這一行的話，唯一能發現的
#   方法是有人剛好去看 /internal/idle-gap-stats。
if notifications.enabled():
    print("[info] 通知信已啟用（Brevo）", flush=True)
else:
    print(
        "[warn] BREVO_API_KEY 未設定，通知信停用——不會寄出任何信件。",
        flush=True,
    )

# LINE 是每家店各自的（上面那一行有寫每家店的狀態）。沒設定時整包停用，
# 而「停用」在外面看起來跟「正常」一模一樣——群組裡 tag bot 沒反應，沒有人
# 會知道是憑證沒設，所以每次啟動都要印出來。

# Flask 預設會把 JSON 物件的鍵照字母排序輸出。對大部分回應無所謂，但
# 管理員場次管理的 tabs 是「照營運流程排的子頁籤順序」
# （待主持確認 → 待收訂金 → 已成立 → 已結束 → 已取消），排序過會變成
# booked, cancelled, ended... 前端照著畫就是亂的。關掉排序，讓後端
# 定義的順序原樣傳到前端。
app.json.sort_keys = False

# 啟動時把每家店的資料表建好（建表本身在 db/schema.ensure_ready，每家店第一次
# 連線時跑）。
#
# 放在模組層而不是 __main__：正式環境用 gunicorn 之類的 WSGI 伺服器啟動時
# 不會執行 __main__。
#
# ★ 兩種失敗分開處理：
#   - **連不上**某家店的資料庫（Neon 暫時不通、正在冷啟動太久）：印警告、
#     繼續啟動。那家店第一次被請求時會再試一次，其他店不受影響——一家店的
#     資料庫出狀況，不該讓所有店一起起不來。
#   - **連得上但建表失敗**（遷移寫錯）：讓服務起不來。那是程式錯誤，而且多半
#     每家店都會中；起不來的話 Render 會保留舊版本，現有服務不中斷，而且
#     一眼就看得到。
import psycopg  # noqa: E402

for _t in tenancy.all_tenants():
    with tenancy.use(_t):
        try:
            psycopg.connect(_t.database_url, connect_timeout=10).close()
        except psycopg.OperationalError as _exc:
            print(
                f"[warn] 店家 {_t.id} 的資料庫現在連不上（{type(_exc).__name__}）。"
                f"先略過，第一次有請求時再建表。",
                flush=True,
            )
            continue
        with pool.connection():
            pass  # 第一次連線會跑 ensure_ready，建表失敗就在這裡拋出來


def bad_request(message: str):
    """格式層的參數錯誤：回 400，並且記一筆。

    ★ 只給「前端根本送不出來」的錯誤用——start=abc、未知頁籤、日期格式
      壞掉。這些是有人在手動戳 API 的訊號。

    ★ 業務層的拒絕不要走這裡：電話格式打錯、時段衝突、聯絡資料沒填，
      那些都是真人在正常操作中會遇到的，一律用 jsonify(...), 400。
      混在一起數的話，一個手機號碼打錯三次的客人會被當成攻擊者。
    """
    if getattr(g, "user", None):
        _record_bad_request(g.user, message)
    return jsonify({"error": message}), 400


def _record_bad_request(user: dict, message: str) -> None:
    # 管理員不記點，理由同速率限制（見 decorators.py）。
    if user.get("role", 3) <= 1:
        return
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # record() 內部會在「開了新的一列」時自動加點，點數到門檻
                # 就把狀態改成 banned（見 auth_utils/guard.py）。這裡負責
                # 善後：把人踢下線、清權限快取。
                #
                # 那兩件事不放進 guard 是因為循環相依——sessions 與
                # whitelist 都會 import guard。
                before = user.get("status")
                guard.record(cur, user["id"], guard.KIND_BAD_REQUEST,
                             request.path, message)
                _revoke_if_banned(cur, user["id"], user["email"], before)
                conn.commit()
    except Exception:  # noqa: BLE001 - 觀測失敗不該影響主要流程
        pass


def _revoke_if_banned(cur, user_id: int, email: str, before_status: str | None) -> None:
    """剛剛那次記點如果導致停權，把人踢下線並清掉權限快取。

    ★ 兩件事一定要一起做（zh-cn-to-tw 的教訓）：只刪 session 的話，對方可以
      立刻重新登入——/auth/login 走的是同一支帶快取的權限查詢，命中時仍然
      回「active」，於是又拿到新 session、繞回原點。
    """
    cur.execute("SELECT status FROM users WHERE id = %s", (user_id,))
    row = cur.fetchone()
    if row is None or row[0] != "banned" or before_status == "banned":
        return
    sessions.delete_sessions_for_email(cur, email)
    whitelist.invalidate(email)


@app.get("/api/health")
def health():
    """健康檢查，同時是保活的目標端點。

    ★ 刻意不碰資料庫。這支要能在「後端醒著但資料庫還在冷啟動」時仍然
      快速回應，否則它就無法用來分辨「服務掛了」與「資料庫比較慢」。

    保活排程（cron-job.org）打這一支。注意免費方案每月只有 750 instance
    hours，24/7 保活會在 31 天用掉 744 小時、幾乎沒有餘裕，超過會讓整個
    workspace 的免費服務停用到下個月——保活範圍要收窄，不要全天候。
    """
    return jsonify({"status": "ok"})


def _job_token_error():
    """排程與統計端點的通行證檢查。通過回 None，不通過回錯誤回應。

    token 是**這家店**的 JOB_TOKEN。每家店一組：一家店的 token 外流，
    只能觸發那一家的排程。沒設就一律拒絕——預設關閉，不是預設開放。

    ★ 用 hmac.compare_digest 而不是 ==：字串比較在第一個不同的字元就回傳，
      回應時間會洩漏「對了幾個字元」（同 line_utils/signature.py 的理由）。
    """
    expected = tenancy.current().job_token
    if not expected:
        return jsonify({"error": "JOB_TOKEN 未設定，此端點停用"}), 503
    got = request.headers.get("X-Job-Token") or ""
    if not hmac.compare_digest(got.encode(), expected.encode()):
        return jsonify({"error": "未授權"}), 401
    return None


@app.post("/api/jobs/daily")
def run_daily_jobs():
    """每日排程的觸發點（cron-job.org 每天 00:00 打這一支，每家店一支）。

    用 JOB_TOKEN 保護，不掛 require_role：這支不是由人呼叫的，沒有登入
    session 可用。

    任何一段出錯就回 500，讓排程服務按它自己的設定重試。重跑是安全的
    （見 jobs.py），所以重試不會有副作用。
    """
    error = _job_token_error()
    if error:
        return error

    result = jobs.run_daily(pool)
    return jsonify(result), (500 if result.get("errors") else 200)


@app.post("/api/jobs/weekly")
def run_weekly_jobs():
    """每週排程的觸發點：異常點數遞減。

    跟每日排程分開一支端點而不是塞進 /api/jobs/daily：兩者的頻率不同，
    而排程服務是用「一個工作 = 一個網址」在管的。合在一起就沒辦法一天跑
    一次那幾支、一週跑一次這一支。

    保護方式與重跑安全性都跟每日排程相同（見 jobs.py）。
    """
    error = _job_token_error()
    if error:
        return error

    result = jobs.run_weekly(pool)
    return jsonify(result), (500 if result.get("errors") else 200)


@app.post("/line/webhook")
def line_webhook():
    """LINE Messaging API 的 webhook。

    ## 這支沒有登入，簽章就是把關

    網址是公開的、沒有 session。唯一能證明「這包資料真的來自 LINE」的是
    X-Line-Signature。驗不過一律 401——否則任何人都能 POST 一包假事件
    進來，在店家的系統上訂出場次。

    ## 5 秒

    LINE 的逾時是 5 秒，超過就當失敗並重送。所以這裡只做非做不可的事：
    驗簽、查劇本、寫入、回覆。推播丟背景佇列。
    真正的風險不在這段程式，在 Render 免費方案的冷啟動（約 60 秒）——
    那靠 24/7 保活解決。

    ## 一律回 200（除了驗簽失敗）

    ★ 處理單一事件失敗時**不要**回 5xx。LINE 會重送整包，而那包裡可能
      有已經處理成功的事件——雖然 request_id 擋得住重複訂場，但重送也
      會重新回覆一次，群組裡就會出現兩句一樣的話。失敗記 log，回 200。
    """
    if not line_client.enabled():
        return jsonify({"error": "LINE 未設定"}), 503

    raw = request.get_data()
    # ★ 一定要用原始 bytes。先 json.loads 再 dumps 會改變空白與鍵序，
    #   雜湊就對不上——症狀是「設定明明都對卻一直 401」。
    if not line_signature.verify(
        raw, request.headers.get("X-Line-Signature"), line_client.channel_secret()
    ):
        return jsonify({"error": "簽章驗證失敗"}), 401

    body = request.get_json(silent=True) or {}
    for event in body.get("events") or []:
        try:
            _handle_line_event(event)
        except Exception as exc:  # noqa: BLE001 - 見上面「一律回 200」
            app.logger.warning("LINE 事件處理失敗：%s", exc, exc_info=True)
    return jsonify({"ok": True})


def _handle_line_event(event: dict) -> None:
    """一則事件。寫入與回覆分開：commit 之後才回，才不會回了卻沒存進去。

    兩條路**資格一樣**——只有綁定過 LINE 的店家人員（見 is_staff）能用：

        群組  tag 我    其他人不管傳什麼都回 no_permission（沒有權限，請聯繫店主）
        私訊  私訊我    同上；只有「我的ID」誰都能問（綁定要先拿到這個 id）

    ★ 私訊這條是因為 **LINE for Mac 的 @ 選單不列官方帳號**（2026-09-14
      實測，手機版可以），而店主就是用 Mac。私訊不需要 tag，Mac 上通。

    訂到之後兩條路做一樣的事，見 _after_line_booking。
    """
    source = event.get("source") or {}
    msg = event.get("message") or {}
    reply_token = event.get("replyToken")

    if event.get("type") != "message" or msg.get("type") != "text":
        return

    # ── 私訊 ──
    if source.get("type") == "user":
        text = (msg.get("text") or "").strip()
        sender = source.get("userId")

        # 拿自己的 userId。推播與私訊訂場都要先綁定這個 id，而它只有在對方
        # 跟 bot 有互動之後才拿得到。任何人都能問——回的是「你自己的 id」，
        # 不是誰的秘密。
        if text.replace(" ", "").upper() in ("我的ID", "MYID"):
            if reply_token and sender:
                line_client.reply(reply_token, f"你的 LINE userId：\n{sender}")
            return

        with pool.connection() as conn:
            with conn.cursor() as cur:
                # 權限在 handle_command 裡判斷，群組那條也是同一段。
                reply, result = line_handler.handle_command(
                    cur, text, event["webhookEventId"], sender=sender
                )
                outbox = _after_line_booking(cur, result, sender)
            conn.commit()
        if reply_token:
            line_client.reply(reply_token, _line_messages(reply))
        notify_events.send(outbox)
        return

    # ── 群組 ──
    if not line_handler.is_group_text_to_me(event):
        # 沒 tag 我就完全不反應——不回覆、不寫入、不記 log。群組裡大部分
        # 訊息都跟 bot 無關，每一則都留一筆 log 只會把真的錯誤淹掉。
        return

    with pool.connection() as conn:
        with conn.cursor() as cur:
            reply, result = line_handler.handle_group_message(cur, event)
            outbox = _after_line_booking(cur, result, source.get("userId"))
        conn.commit()

    if reply_token:
        line_client.reply(reply_token, _line_messages(reply))
    # ★ commit 之後才推播、才寄信，跟網站那幾條路同一條規則：交易回捲時
    #   不該有人已經收到「訂好了」。
    notify_events.send(outbox)


def _line_messages(reply):
    """handler 回的東西 → line_client.reply 收的東西。

    多半是一句話，原樣交出去。查詢指令（某一天、行事曆）回的是 ImageReply：
    這裡組出這個後端自己的圖片網址，LINE 再照網址來抓（見 line_image）。
    webhook 這一趟只簽一個網址，不畫圖——畫圖是 LINE 來抓的那一趟的事，
    不佔這裡 5 秒的預算。

    ★ https 寫死：LINE 只收 https。TLS 在 Render 前端就解掉了，這個 process
      看到的 request.scheme 可能是 http——照它組的話網址會是 http://，LINE 不收。
    ★ 預覽圖與原圖用同一個網址：預覽上限 1MB，這裡的圖最大的（十二個月）
      也才幾十 KB，不必另外畫一張小的。
    """
    if not isinstance(reply, line_images.ImageReply):
        return reply
    url = url_for(
        "line_image",
        token=line_images.sign(reply.spec, line_client.channel_secret()),
        _external=True,
        _scheme="https",
    )
    return [{"type": "image", "originalContentUrl": url, "previewImageUrl": url}]


@app.get("/line/img/<token>.png")
def line_image(token: str):
    """LINE 照 _line_messages 發出去的網址來抓圖。

    ## 沒有登入，簽名就是把關

    來抓的是看訊息的那台裝置上的 LINE App（實測是 Mac 版，見 images.py），
    不是 LINE 的伺服器，也不會帶任何登入資訊。能證明
    「這張圖是發給店家人員的」只有網址上的簽名：發網址之前已經確認過發問的
    人是店家人員，簽名確保網址上的日期沒被改過。驗不過一律 404。

    ## 每次都照當下的資料現畫

    理由與那一行 [line-img] log 見 line_utils/images.py 的開頭。
    """
    if not line_client.enabled():
        return jsonify({"error": "LINE 未設定"}), 503
    checked = line_images.verify(token, line_client.channel_secret())
    if checked is None:
        return jsonify({"error": "網址無效"}), 404
    spec, issued = checked
    with pool.connection() as conn:
        with conn.cursor() as cur:
            png = line_images.render(
                cur, spec, issued=issued, user_agent=request.headers.get("User-Agent", "")
            )
    resp = app.response_class(png, mimetype="image/png")
    # 網址含發出時間，每一次查詢都是新的網址，所以快取住也不會把別次的圖給
    # 錯人；被快取住的就是第一次抓到的那張，跟圖上的「截至幾點」一致。
    # 快取留得愈久，LINE 愈不需要回來抓——也就愈少碰到這裡正在休眠的時候。
    resp.headers["Cache-Control"] = "public, max-age=31536000"
    return resp


def _after_line_booking(cur, result, sender: str | None) -> list:
    """LINE 訂到場之後要發的東西（信與推播），交給 notify_events.send()。在 commit 之前呼叫。

    ★ 沒訂到（沒權限、格式錯、撞場…）什麼都不發。

    ★ LINE 重送同一個事件時 create_booking 回 already_existed——那一筆第一次
      就發過了，這次什麼都不重發，不然店家會收到兩次一樣的推播與信。

    ★ 跟網站、後台走同一支 notify_events.on_booking_change：要發什麼、發給誰，
      規則只有那一份。做這個動作的人是發訊者綁定的那個帳號，推播會排除他。
    """
    if result is None or result.already_existed:
        return []
    booking = bookings_db.get_booking(cur, result.id)
    return notify_events.on_booking_change(
        cur, None, booking, actor_id=line_handler.user_id_for(cur, sender)
    )


@app.get("/internal/idle-gap-stats")
def idle_gap_stats():
    """資料庫存取間隔統計，用來估 Neon 的 CU-hours。

    ★ 這支刻意不碰資料庫，也刻意不掛 require_auth：任何要查資料庫的守衛
      都會讓「查看統計」這個動作自己算成一次存取——把間隔壓短，還順便把
      compute 叫醒。量測工具污染被量測的對象，數字就沒有意義了。
      所以改用跟每日排程同一組的 X-Job-Token，那只是比對一個環境變數。

    沒設 JOB_TOKEN 就整支關閉，跟 /api/jobs/daily 一樣預設關著。
    數字都是**這家店**的（每家店是自己的 Neon 專案，各自休眠）。
    """
    error = _job_token_error()
    if error:
        return error
    return jsonify({**idle_gap.stats(), "slot_cache": slot_cache.stats(),
        "notifications": notifications.stats(), "intro": intro_public.stats()})


@app.get("/api/ready")
def ready():
    """連資料庫一起檢查。部署後確認整條路徑通了用的，不要拿來當保活目標
    （那會讓每次保活都把 Neon 喚醒，把免費的 CU-hours 額度燒掉）。

    ★ 要帶這家店的 X-Job-Token（2026-10-25，known-issue-check 第 33 條）。原本
      誰都能打，而它每次都會連資料庫——任何人每 5 分鐘打一次，那家店的 Neon
      就永遠不睡、月額度用完整家店停擺。改成跟 /internal/idle-gap-stats 同一組
      通行證：它本來就是給維運的人看的，不是給使用者的。
    """
    error = _job_token_error()
    if error:
        return error
    # ★ 只回報「有沒有設定」，不回報值。設定錯誤最難查的地方在於它跟
    #   「憑證真的無效」回同一句 401——部署後光看錯誤訊息分不出是哪一種。
    #   這裡把「設定面」攤開，但不洩漏任何秘密。都是**這家店**的設定。
    tenant = tenancy.current()
    config = {
        "google_client_id": bool(tenant.google_client_id),
        "allowed_origins": list(tenant.allowed_origins) or "（未設定，放行全部）",
        "job_token": bool(tenant.job_token),
        "line": tenant.line_enabled,
    }
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return jsonify({"status": "ok", "database": "ok", "config": config})
    except Exception as exc:  # noqa: BLE001 - 回報狀態，不讓例外變成 500
        # ★ 連線池只會說「等不到連線」，把真正的原因吞掉——那句話對排查
        #   完全沒有幫助，因為「密碼錯」「主機名打錯」「SSL 沒開」「Neon
        #   在冷啟動」全都長這樣。這裡直接繞過池子連一次，把底層的例外
        #   attempt 出來。
        #
        #   不會洩漏密碼：Postgres 的錯誤訊息只講失敗原因與主機，連線字串
        #   本身不會出現在裡面。為保險起見仍然只取例外的類別與訊息，
        #   不回傳任何設定值。
        # ★ 池子的內部統計。這是唯一能分辨「有在嘗試連線但失敗」與
        #   「根本沒在嘗試」的東西——後者代表背景 worker 沒在運作，
        #   那是完全不同的問題，而 PoolTimeout 對兩者是同一句話。
        stats = pool.get_stats()
        detail = None
        try:
            with psycopg.connect(tenant.database_url, connect_timeout=10) as _c:
                with _c.cursor() as _cur:
                    _cur.execute("SELECT 1")
                    _cur.fetchone()
            detail = "直連成功——問題出在連線池，不是資料庫本身"
        except Exception as inner:  # noqa: BLE001
            detail = f"{type(inner).__name__}: {inner}"
        return jsonify({
            "status": "degraded",
            "database": str(exc),
            "database_detail": detail,
            "pool_stats": stats,
            "config": config,
        }), 503


@app.post("/auth/login")
def login():
    """用 Google ID Token 換一張我們自己的 session token。

    ★ 這是全站唯一驗證 Google ID Token 的地方。之後所有請求都用回傳的
      session_token，不要再送 Google 的 token。理由見 auth_utils/google_auth.py。
    """
    data = request.get_json(silent=True) or {}
    id_token = data.get("id_token")
    if not id_token:
        return jsonify({"error": "缺少 id_token"}), 400

    email = verify_google_id_token(id_token)
    if email is None:
        return jsonify({"error": "Google 憑證無效"}), 401

    with pool.connection() as conn:
        with conn.cursor() as cur:
            # ★ fresh=True 在這裡是必要的，不是保險。
            #
            # 停用帳號的流程是「改 status + 刪掉該帳號所有 session」，讓對方
            # 立刻被登出。但如果簽發新 session 的這一步讀的是 30 秒快取，
            # 對方被踢出去之後可以馬上重新登入、拿到一張全新的 session——
            # 刪 session 那一步就完全白做了，停用還是要等快取過期才生效。
            #
            # 換句話說：撤銷的效果取決於「重新取得憑證」這條路徑有多新，
            # 而不是取決於「舊憑證被刪掉」。登入頻率低，多一次查詢沒有成本。
            #
            # 這個縫是 zh-cn-to-tw 實作停用撤銷時踩到並回報的。
            user = whitelist.get_user(cur, email, fresh=True)
            # ★ 被停權的擋在這裡，而且要擋在發 session 之前。
            #   放行的話他手上會有一張有效憑證，就算每支路由都會再擋一次，
            #   那也是多開了一道不需要存在的門。
            # ★ 已刪除的帳號不發 session。他刪掉自己之後除了找店家沒有
            #   別的路，發一張只能用來看空畫面的憑證沒有意義，還得多存
            #   一份不該存的東西。
            if user is not None and user["status"] == "deleted":
                return jsonify({
                    "error": "此帳號已刪除", "code": "deleted", "email": email,
                }), 403

            # 被停權的照樣發。他要能進到畫面看見「未授權」這件事本身——
            # 直接擋在登入頁只會讓他一直重登。每一支受保護的路由仍然會
            # 擋下他，這張憑證換不到任何資料。

            # 沒在名單裡的照樣發 session。他的身分 Google 已經驗過了，
            # 缺的只是聯絡資料——那要靠 /auth/register 補，而註冊本身就需要
            # 一張憑證來證明「填這份表的人就是剛才登入的那個 email」。
            token = sessions.create_session(cur, email)
            has_contact = (
                profile.has_complete_contact(profile.get(cur, user["id"]))
                if user is not None else False
            )
            conn.commit()

    if user is None:
        return jsonify({
            "session_token": token,
            "email": email,
            "registered": False,
        })

    if user["status"] != "active":
        return jsonify({
            "session_token": token,
            "email": user["email"],
            "registered": True,
            "authorized": False,
            "status": user["status"],
        })

    return jsonify(
        {
            "session_token": token,
            "email": user["email"],
            "name": user["name"],
            "role": user["role"],
            "is_admin": user["role"] == 1,
            "registered": True,
            "authorized": True,
            "status": user["status"],
            "has_contact": has_contact,
        }
    )


@app.post("/auth/logout")
def logout():
    """登出。沒帶 token 或 token 不存在都視為成功——呼叫端要的結果
    （這張憑證不再有效）本來就已經成立。"""
    token = extract_bearer_token(request)
    # 偽造的 token 不開連線（見 sessions.is_authentic）；對呼叫端而言結果一樣。
    if sessions.is_authentic(token):
        with pool.connection() as conn:
            with conn.cursor() as cur:
                sessions.delete_session(cur, token)
                conn.commit()
    return jsonify({"status": "ok"})


@app.get("/auth/status")
def auth_status():
    """查詢目前的登入狀態。

    ★ 刻意不掛 require_auth：「查過了，發現沒有授權」是一個正常的查詢結果，
      不是錯誤。前端載入時要用這支決定畫面要不要上鎖，如果它自己就會回
      403，前端得靠攔截錯誤來判斷狀態，那很容易寫錯。

    只有在完全沒帶 token 或 token 無法解析時才回 401。
    """
    token = extract_bearer_token(request)
    if not token:
        return jsonify({"error": "未提供登入憑證"}), 401
    # 偽造的 token 不開連線（見 sessions.is_authentic）。
    if not sessions.is_authentic(token):
        return jsonify({"error": "登入憑證無效或已過期"}), 401

    with pool.connection() as conn:
        with conn.cursor() as cur:
            email = sessions.resolve_session(cur, token)
            if email is None:
                conn.commit()
                return jsonify({"error": "登入憑證無效或已過期"}), 401
            user = whitelist.get_user(cur, email)
            # 聯絡資料是否齊全。只回一個布林值，不回內容——前端只需要知道
            # 「能不能訂場」，而 line_id/phone 也不該進權限快取（那份快取
            # 在每個請求的熱路徑上，聯絡資料只有兩個畫面要用）。
            has_contact = False
            if user is not None:
                has_contact = profile.has_complete_contact(profile.get(cur, user["id"]))
            conn.commit()

    authorized = bool(user and user["status"] == "active")
    return jsonify(
        {
            "authorized": authorized,
            # registered 與 authorized 是兩件事：沒註冊的人不算授權，但他
            # 該看到的是註冊表單；被停權的人也不算授權，他該看到的是
            # 「請聯絡店家」。前端靠這個欄位分流。
            "registered": user is not None,
            # 前端要靠這個分辨「停權」與「已刪除」——兩者都是 authorized
            # false，但畫面與訊息不同。
            "status": user["status"] if user else None,
            # status == "banned" 時，停權畫面要顯示的兩件事。
            #
            # ★ 到期時間一定要給。只說「你被停權了」，當事人不知道該等一下
            #   還是該打電話——而 5 分鐘跟 30 天的處置完全不同。
            #   None 代表無限期（abuse_rule 的 ban_seconds 設 0），只能找管理員。
            "banned_until": (
                user["banned_until"].isoformat()
                if user and user.get("banned_until") and user["status"] == "banned"
                else None
            ),
            "admin_name": admin_display_name() if user and user["status"] == "banned" else None,
            "email": user["email"] if user else email,
            "name": user["name"] if user else None,
            "role": user["role"] if user else None,
            "is_admin": bool(user and user["role"] == 1),
            "has_contact": has_contact,
        }
    )


@app.post("/auth/register")
def register():
    """自助註冊。用登入時拿到的 session token 認身分，補齊聯絡資料。

    ★ 刻意不掛 require_auth：那支要求「名單裡有這個人且是 active」，而這
      支存在的目的正是「名單裡還沒有這個人」。這裡只驗 session 有效，
      email 取自 session，不取請求內容。

    註冊完成後原本那張 session 立刻可用——email 沒變，只是 users 多了一
    列，下一次 require_auth 查就會通過。不需要重新登入。
    """
    token = extract_bearer_token(request)
    if not token:
        return jsonify({"error": "未提供登入憑證"}), 401
    # 偽造的 token 不開連線（見 sessions.is_authentic）。
    if not sessions.is_authentic(token):
        return jsonify({"error": "登入憑證無效或已過期"}), 401

    body = request.get_json(silent=True) or {}
    with pool.connection() as conn:
        with conn.cursor() as cur:
            email = sessions.resolve_session(cur, token)
            if email is None:
                conn.commit()
                return jsonify({"error": "登入憑證無效或已過期"}), 401
            try:
                user = whitelist.register_self(cur, email, body)
            except ValueError as exc:
                conn.rollback()
                return jsonify({"error": str(exc)}), 400
            conn.commit()

    return jsonify({
        "email": user["email"],
        "name": user["name"],
        "role": user["role"],
        "is_admin": user["role"] == 1,
        "registered": True,
    }), 201


@app.get("/api/me")
@require_auth
def me():
    """目前登入者的資料。也是驗證 require_auth 有沒有正常運作的最小路由。"""
    return jsonify(g.user)


@app.get("/api/profile")
@require_auth
def get_profile():
    """自己的完整資料。比 /api/me 多了聯絡欄位——那兩欄不放進權限快取，
    因為快取在每個請求的熱路徑上，而聯絡資料只有這一頁要用。"""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            data = profile.get(cur, g.user["id"])
    return jsonify(data)


@app.put("/api/profile")
@require_auth
def update_profile():
    """改自己的聯絡資料。只吃 name / line_id / phone，其餘欄位忽略。"""
    body = request.get_json(silent=True) or {}
    errors = profile.validate_contact(body)
    if errors:
        return jsonify({"error": "格式不正確", "fields": errors}), 400
    with pool.connection() as conn:
        with conn.cursor() as cur:
            data = profile.update(cur, g.user["id"], body)
            conn.commit()
    return jsonify(data)


@app.post("/api/profile/delete")
@require_auth
def delete_profile():
    """刪除自己的帳號。還有已成立的場次就拒絕。"""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            try:
                cancelled = profile.delete_self(cur, g.user["id"])
            except profile.HasActiveBookings:
                conn.rollback()
                return jsonify({
                    "error": "你尚有未進行的場次，請與店家聯繫，或場次結束後再刪除",
                    "code": "has_active_bookings",
                }), 409
            # 帳號沒了，手上那張憑證也不該還能用。
            sessions.delete_sessions_for_email(cur, g.user["email"])
            conn.commit()
    return jsonify({"deleted": True, "cancelled_bookings": cancelled})


@app.get("/api/admin/ping")
@require_role(1)
def admin_ping():
    """僅管理員。用來驗證階層式權限判斷。"""
    return jsonify({"status": "ok", "as": g.user["email"]})


# ── 劇本 ────────────────────────────────────────────────────────

@app.get("/api/mmg")
@require_auth
def list_mmg():
    """可預約的劇本清單，含每個主持人角色有哪些人可帶。"""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            return jsonify(mmg_db.list_active(cur))


@app.get("/api/mmg/<int:mmg_id>")
@require_auth
def get_mmg(mmg_id: int):
    with pool.connection() as conn:
        with conn.cursor() as cur:
            item = mmg_db.get(cur, mmg_id)
    if item is None:
        return jsonify({"error": "找不到這個劇本"}), 404
    return jsonify(item)


# ── 預約 ────────────────────────────────────────────────────────

def _parse_slot(source) -> tuple:
    """從 request 參數取出 (date, time)，格式錯誤丟 BookingError。"""
    raw_date = (source.get("session_date") or "").strip()
    raw_time = (source.get("session_time") or "").strip()
    if not raw_date or not raw_time:
        raise BookingError("需要 session_date 與 session_time")
    try:
        d = date.fromisoformat(raw_date)
    except ValueError:
        raise BookingError(f"日期格式錯誤：{raw_date}（需要 YYYY-MM-DD）") from None
    try:
        # 允許 HH:MM 與 HH:MM:SS 兩種寫法
        t = time.fromisoformat(raw_time if len(raw_time) > 5 else raw_time + ":00")
    except ValueError:
        raise BookingError(f"時間格式錯誤：{raw_time}（需要 HH:MM）") from None
    return d, t


@app.get("/api/mmg/<int:mmg_id>/availability")
@require_auth
def get_availability(mmg_id: int):
    """一段期間內，這個劇本哪些日期開放、各自有哪些時段。

    給前端的日期／時間選單用：只有這裡回報的日期選得到，選了日期之後
    時間下拉也只列這裡給的時段。玩家因此不會選到一個註定被退回的時間。

    只回傳有開的日期。一個月裡不開的日子通常比開的多，回一堆
    is_open=false 的空殼只是把同一份資訊講得更長。
    """
    try:
        start = date.fromisoformat(request.args.get("from", ""))
        end = date.fromisoformat(request.args.get("to", ""))
    except ValueError:
        return bad_request("from 與 to 必須是 YYYY-MM-DD")
    if end < start:
        return bad_request("to 不能早於 from")
    # 一次最多查一季。沒有上限的話，一個 from=1900&to=2999 的請求會讓
    # 後端逐日跑一百萬圈——這支不碰資料庫做迴圈，但迴圈本身就夠久了。
    if (end - start).days > 92:
        return bad_request("一次最多查 92 天")

    with pool.connection() as conn:
        with conn.cursor() as cur:
            if mmg_db.get(cur, mmg_id) is None:
                return jsonify({"error": "找不到這個劇本"}), 404
            days, configured = schedule.calendar(cur, mmg_id, start, end)
    return jsonify({
        "mmg_id": mmg_id,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "days": days,
        # 「還沒設定過」與「設定成全關」的 days 都是空的，但意思完全不同。
        # 前端要據此決定顯示「店家尚未開放此劇本的預約時段」還是放行——
        # 沒設定過的劇本不該因為這個功能上線就整個訂不了。
        "configured": configured,
    })


@app.get("/api/mmg/<int:mmg_id>/slot")
@require_auth
def get_slot(mmg_id: int):
    """某個劇本某個時段目前的排隊狀況。

    這是開賣尖峰時最熱的讀取路徑（大量玩家重複整理看還有沒有位子），
    所以整個回應有快取——見 db/slot_cache.py。寫入路徑一步都不動，
    精準性仍然由 Postgres 的唯一性約束保證。

    ★ 快取的是「這個時段的狀況」，跟問的人是誰無關：下面回傳的欄位裡
      沒有任何一個跟使用者有關，所以跨使用者共用是安全的。哪天要在這支
      加上會因人而異的欄位（例如「你自己排在第幾位」），快取就必須跟著
      改成 per-user，否則會把 A 的資訊回給 B。
    """
    try:
        d, t = _parse_slot(request.args)
    except BookingError as exc:
        return jsonify({"error": str(exc)}), 400

    ck = slot_cache.key(mmg_id, d, t)
    cached = slot_cache.get(ck)
    if cached is not None:
        # ★ 命中就直接回，連線都不開——這正是要省的東西：不是那幾毫秒，
        #   是「不要把 Neon 從休眠叫醒」。
        return jsonify(cached)

    with pool.connection() as conn:
        with conn.cursor() as cur:
            item = mmg_db.get(cur, mmg_id)
            if item is None:
                return jsonify({"error": "找不到這個劇本"}), 404
            queue = bookings_db.list_slot(cur, mmg_id, d, t)
            # ★ 這一支也要問撞期，不能只回報排隊人數。
            #   容量沒滿不代表訂得成——包廂可能被別齣戲佔著。少了這個，
            #   玩家會看到「可進行預約」、把主持人也選完，直到按下送出
            #   才被 create_booking() 退回來。錯誤要出現在他還能改的時候。
            clash = bookings_db.has_room_conflict(cur, mmg_id, d, t)
            # ★ 主持人卡場也要在這裡問，不能只問包廂。
            #
            #   一人店（gm_mode=single）通常同時是 conflict_room=off——
            #   她沒有包廂的概念。那樣上面那一問永遠回 None，/slot 就永遠
            #   說「可進行預約」，玩家把日期時間填完、按下送出，才被
            #   create_booking() 用「某某在這個時間有場次」退回來。
            #   **那正是這支端點存在要防的事。**
            #
            #   conflicting_gm 自己會判斷：single 模式下主持人是已知的
            #   （就是店主），傳空陣列照樣查得出來；assign／option 模式
            #   下主持人是玩家在畫面上挑的，這一刻還不知道是誰，那支會
            #   直接回 None——那兩種模式仍然靠 /api/bookings/check-gm
            #   在玩家選人的當下即時問。
            gm_clash = bookings_db.conflicting_gm(cur, [], mmg_id, d, t)
            # 排期跟容量、撞期是三件不同的事，各自回一個旗標，訊息的
            # 優先序交給前端排（見 booking.js 的 slotMessage）。
            slot_open = schedule.is_open(cur, mmg_id, d, t)
            # ★ 忙碌日單獨回一個旗標，不只靠 is_open。兩者都會讓人訂不成，
            #   但說法完全不同：「此時間未開放」會讓玩家去試別的時間，而
            #   那天不管幾點都不行。
            store_busy = busy_db.store_busy(cur, d)

    capacity = item["waitlist_limit"]
    # 這個時段有沒有已經成立的那一組。玩家看到的說法差很多：已經有人
    # 成立了，後面排的就是候補；都還沒成立的話，大家只是先後順序不同。
    has_booked = any(st == "booked" for _p, _b, _n, st, _d, _t2, _r in queue)
    payload = {
        "mmg_id": mmg_id,
        "mmg_name": item["name"],
        "session_date": d.isoformat(),
        "session_time": t.strftime("%H:%M"),
        "capacity": capacity,
        "taken": len(queue),
        "has_booked": has_booked,
        "is_full": len(queue) >= capacity,
        # 這個時段在不在店家設定的開放範圍內。不開放時前面那幾個數字
        # 就沒有意義了——沒開放的時段談「還有幾個位子」是空的。
        "is_open": slot_open,
        # 這天是不是店家忙碌日。前端據此換一句話（見 booking.js 的 slotMessage）。
        "store_busy": store_busy,
        # ★ 只回一個布林值，不回是哪一場撞到。
        #   玩家不需要知道包廂裡在演什麼、誰訂的、演到幾點——那是別的
        #   客人的預約。前端就算不顯示，送出去的東西打開開發者工具就
        #   看得到，所以不顯示的資訊也不要送。
        #   管理員與主持人要看細節，走 /api/bookings/<id>/conflicts，
        #   那一支有 role <= 2 的把關。
        "has_conflict": clash is not None or gm_clash is not None,
        # ★ 刻意不回排隊清單。玩家需要知道的只有「排了幾組、我會是第幾個、
        #   訂不訂得成」——前面排的是誰、他們的預約在什麼狀態，那是別的
        #   客人的資料，跟他要做的決定無關。
        #   管理員與主持人的畫面各自有完整清單，那兩支都有權限把關。
    }
    slot_cache.put(ck, payload)
    return jsonify(payload)


@app.get("/api/bookings/check-gm")
@require_auth
def check_gm_route():
    """玩家在預約畫面選了某位主持人時，即時確認他這個時段有沒有空。

    跟送出時的檢查是同一個函式（conflicting_gm），只是提早跑一次，
    讓玩家在填表當下就知道，而不是全部填完按下送出才被退回。這裡不做
    任何寫入，也不取任何鎖——真正的把關仍然在 create_booking() 裡，
    那裡才有 advisory lock 保證「檢查到寫入」之間沒有人插隊。
    """
    try:
        mmg_id = int(request.args.get("mmg_id", ""))
        user_id = int(request.args.get("user_id", ""))
    except ValueError:
        return bad_request("需要 mmg_id 與 user_id")

    try:
        d, t = _parse_slot(request.args)
    except BookingError as exc:
        return jsonify({"error": str(exc)}), 400

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM mmg WHERE id = %s", (mmg_id,))
            if cur.fetchone() is None:
                return jsonify({"error": "找不到這個劇本"}), 404

            # 忙碌日排在撞期前面：兩者都擋，但一句是「他那天有場次」、
            # 一句是「他那天整天不接」，玩家要做的事不一樣。
            if busy_db.busy_gm_ids(cur, [user_id], d):
                cur.execute("SELECT name FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
                who = (row[0] if row else None) or f"id={user_id}"
                return jsonify({
                    "error": f"{who}這天不開放指定任何劇本，請選擇其他主持人或改期。",
                    "code": bookings_db.REASON_GM_BUSY,
                }), 409
            conflicting = bookings_db.conflicting_gm(
                cur, [user_id], mmg_id, d, t
            )
            if conflicting is None:
                return jsonify({"ok": True})

            cur.execute("SELECT name FROM users WHERE id = %s", (conflicting,))
            name_row = cur.fetchone()
            gm_name = name_row[0] if name_row else f"id={conflicting}"

    return jsonify({"error": f"{gm_name}在這個時間有場次無法預訂"}), 409


@app.post("/api/bookings")
@require_auth
def create_booking_route():
    """建立預約。

    request_id 是必填的冪等鍵，前端應該在玩家「按下預約」當下產生一組，
    該次意圖的所有重試都沿用同一組。刻意不允許省略：如果這裡自動產生，
    每次重試都會變成一次新的意圖，冪等保護就形同虛設——玩家網路不穩
    重送一次就可能佔到兩個名額，而名額是有限且被搶的。

    ★ 這支**只取它列出來的那幾個鍵**。請求裡多帶的東西一律不看：訂金是
      店家收到錢才算數、備註是主持人／管理員的欄位、狀態與預訂者更不是
      玩家能決定的。這條規則要靠「後端只讀自己要的欄位」來守，不能靠
      「前端不會送」——任何人都能直接打這支 API。
    """
    body = request.get_json(silent=True) or {}

    request_id = (body.get("request_id") or "").strip()
    if not request_id:
        return bad_request("缺少 request_id（冪等鍵），請由前端產生一組 UUID")

    mmg_id = body.get("mmg_id")
    if not isinstance(mmg_id, int):
        return bad_request("需要 mmg_id")

    try:
        d, t = _parse_slot(body)
    except BookingError as exc:
        return jsonify({"error": str(exc)}), 400

    # ★ 整個省略是允許的，形狀錯才擋。single 模式的店（functions 表的
    #   gm_mode）根本沒有「選主持人」這個畫面——主持人是系統依組態填的，
    #   不是呼叫端挑的（見 db/bookings.create_booking），要求那種店的前端
    #   送一個它沒有概念的空陣列過來只是儀式。
    #
    #   對 assign 模式也沒有放鬆：填成四個 null 之後 _validate_gm_choice
    #   照樣擋，而且訊息更好——「《X》的角色 1「Y」尚未指定主持人」比
    #   「gm_user_ids 需要是長度 4 的陣列」看得懂。真正壞掉的形狀
    #   （字串、長度不對）仍然在這裡就被擋下。
    gm_user_ids = body.get("gm_user_ids")
    if gm_user_ids is None:
        gm_user_ids = [None, None, None, None]
    if not isinstance(gm_user_ids, list) or len(gm_user_ids) != 4:
        return bad_request("gm_user_ids 需要是長度 4 的陣列，沒有的角色填 null")

    # 玩家一律是自己，沒有例外——這裡不存在「代訂」這個動作。任何角色
    # （包含主持人、管理員）建立預約時都是為自己訂。
    #
    # 管理員事後要把某筆預約轉給別人，走的是另一條路：管理員先用自己的
    # 身份正常訂好，之後在管理員介面把這筆既有紀錄的訂場者改掉——那是
    # 「編輯一筆已存在的預約」，不是「建立當下代替別人下單」，兩者刻意
    # 分開，因為後者等於任何有 body 存取權的人都能塞一個 player_id
    # 幫任何人佔位，前者則是有明確操作紀錄、事後可追溯的單一動作。
    # 那支編輯端點屬於管理員介面，尚未實作。
    player_id = g.user["id"]

    # ★ 聯絡資料不完整就不給訂。前端也會擋（訊息比較清楚），但那是化妝品：
    #   任何人都能直接打這支 API。店家聯絡不到人的預約等於沒有預約。
    with pool.connection() as conn:
        with conn.cursor() as cur:
            me = profile.get(cur, g.user["id"])
    if not profile.has_complete_contact(me):
        return jsonify({
            "error": "你沒有填寫基本聯絡資料，無法預訂場次",
            "code": "incomplete_contact",
        }), 400

    with pool.connection() as conn:
        with conn.cursor() as cur:
            # ★ fresh=True：跳過 30 秒權限快取。這是會佔用稀缺資源的寫入，
            #   在快取窗口內成立的預約會真的佔住名額，而停用帳號並不會
            #   把它吐回來。理由詳見 auth_utils/whitelist.get_user。
            if not whitelist.is_permitted_user(cur, g.user["email"], fresh=True):
                return jsonify({"error": "此帳號未獲授權"}), 403
            try:
                result = bookings_db.create_booking(
                    cur,
                    mmg_id=mmg_id, player_id=player_id,
                    session_date=d, session_time=t,
                    gm_user_ids=gm_user_ids,
                    request_id=request_id,
                )
            except BookingError as exc:
                conn.rollback()
                return jsonify({"error": str(exc)}), 409
            booking = bookings_db.get_booking(cur, result.id)
            # ★ 組信在 commit 之前（要 cursor），送出在 commit 之後（見下）。
            #   重試打到既有的那筆時 before 給 booking 自己，兩邊一樣就沒有
            #   任何轉變，不會重寄——否則連點三次「預約」等於主持人收三封。
            outbox = notify_events.on_booking_change(
                cur, booking if result.already_existed else None, booking,
                actor_id=g.user["id"],
            )
            conn.commit()
    notify_events.send(outbox)

    return jsonify({
        **booking,
        "position": result.position,
        "is_first": result.is_first,
        "label": result.label,
        # 前端用 status 與這個值決定要不要講「第 N 序位」（見 web 的 ui.js
        # positionLabel）。欄位名跟「我的預約」那支一致，兩邊共用同一條規則。
        "waitlist_limit": result.capacity,
        # 重試時是 200 而不是 201——這一次沒有真的建立東西。
        "already_existed": result.already_existed,
    }), (200 if result.already_existed else 201)


@app.get("/api/bookings/mine/tabs")
@require_auth
def my_booking_tabs():
    """頁籤清單。只是把後端定義的順序與標籤交給前端，讓兩邊不會各寫一份。

    ★ 給的是「這家店到得了的那幾個」，不是全部五個——理由見
      db/bookings.reachable_tabs。
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            return jsonify({"tabs": bookings_db.reachable_tabs(cur)})


@app.get("/api/bookings/mine/tab/<tab>")
@require_auth
def my_bookings_tab(tab: str):
    """自己在某個狀態下的預約，一次一頁。

    ★ 一次只給一個頁籤，不像管理員那支一口氣回五包。玩家的畫面同一時間
      只看得到一個頁籤，先把另外四包撈出來是替他決定他要看什麼——而每一
      包都是一次查詢。切過去再抓就好。
    """
    try:
        start = int(request.args.get("start", 1))
    except ValueError:
        return bad_request("start 必須是數字")
    if start < 1:
        return bad_request("start 必須大於 0")

    try:
        filters = _parse_filters(request.args)
        with pool.connection() as conn:
            with conn.cursor() as cur:
                return jsonify(bookings_db.list_player_tab(
                    cur, g.user["id"], tab, start=start, filters=filters,
                ))
    except BookingError as exc:
        return jsonify({"error": str(exc)}), 400


# ── 預訂行事曆 ──────────────────────────────────────────────────
#
# 兩支唯讀端點，一支給日曆上的點、一支給點進某一天之後的清單。
#
# ★ 為什麼不是一支。點要畫在整個月上，所以需要「這個月每一天有什麼」；
#   清單只需要一天。合成一支回傳整個月的完整內容的話，一次要背整月所有
#   場次的卡片資料，而使用者一次只看得到一天。分成兩支之後，翻月份只花
#   一包很小的狀態清單，點日期才去拿那一天的內容。
#
# ★ 為什麼不用現成的 /tab/<tab>?session_date=：那支一次只查一個狀態，
#   而行事曆的某一天是五個狀態混在一起，得打五次。


@app.get("/api/bookings/mine/month/<ym>")
@require_auth
def my_bookings_month(ym: str):
    """行事曆上的點：這個月每一天有哪些場次的狀態，依開始時間排序。

    回傳 {"days": {"2026-09-05": ["gm_confirm", "booked"]}}——一場一個值，
    順序就是畫面上那排點由左往右的順序。

    ★ 不含已取消的場次；點進某一天的清單則全部列出（案主定案）。
    """
    try:
        first = date.fromisoformat(f"{ym}-01")
    except ValueError:
        return bad_request("月份格式錯誤，需要 YYYY-MM")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            days = bookings_db.player_month_days(cur, g.user["id"], first.year, first.month)
    return jsonify({"month": ym, "days": days})


@app.get("/api/bookings/mine/date/<d>")
@require_auth
def my_bookings_date(d: str):
    """行事曆上點某一天：那天自己的全部預約，不分狀態、已取消的也列。

    形狀跟 /tab/<tab> 完全一樣（同一份 SELECT、同一個 item），所以前端兩個
    畫面用的是同一張卡片。
    """
    try:
        session_date = date.fromisoformat(d)
    except ValueError:
        return bad_request("日期格式錯誤，需要 YYYY-MM-DD")
    try:
        start = int(request.args.get("start", 1))
    except ValueError:
        return bad_request("start 必須是數字")
    if start < 1:
        return bad_request("start 必須大於 0")

    with pool.connection() as conn:
        with conn.cursor() as cur:
            return jsonify(bookings_db.list_player_date(
                cur, g.user["id"], session_date, start=start,
            ))


def _is_assigned_gm(booking: dict, user_id: int) -> bool:
    """這個人是不是這筆預約指定的主持人之一。"""
    return user_id in (booking.get("gm_user_ids") or [])


# ★ 這裡曾經有兩支寫入路由，2026-09-12 移除：
#
#     POST  /api/bookings/<id>/cancel    任何登入者取消自己的（管理員取消任何一筆）
#     PATCH /api/bookings/<id>/status    管理員直接改狀態
#
#   兩支都出自 2026-08-26 的第一批 API，那時候還沒有管理員介面。後來長出來
#   的畫面兩支都沒用到：玩家的「我預訂的場次」刻意唯讀（取消要經過店家，
#   那支路由自己的說明也是這樣寫的），管理員的取消與改狀態都走場次管理的
#   編輯（PUT /api/admin/bookings/<id>）。
#
#   留著的代價不只是沒人用：改狀態那支是編輯那條路的弱化版重複品——它直接
#   寫 status，繞過「主持人全數確認 → 待收訂金」的自動轉態，也繞過應收訂金
#   有沒有設定的檢查。同一個欄位兩扇門、其中一扇守衛比較少，遲早有人走錯。
#
#   哪天要做「玩家自行取消」，要一起決定的事情比這支多：離場多久前還能取消、
#   訂金退不退、要不要通知已經確認的主持人、序位遞補要不要發信。屆時重寫一支
#   帶著那些規則的，比沿用這支什麼規則都沒有的好。

# ── 主持人介面 ──────────────────────────────────────────────────
#
# 權限一律 require_role(2)：那是「主持人以上」，管理員（role 1）因為
# 階層關係本來就通過（判斷式是 role <= minimum，不是 role == minimum）。
#
# 但這些路由回傳的是「g.user 這個人自己被指定的場次」，不是全站場次——
# 管理員登入主持人介面時，看到的是他自己以主持人身分被指定的那些，
# 不是所有人的。管理員要看全站，是之後管理員介面的事，兩者刻意分開。

def _parse_filters(args) -> dict:
    """從 query string 取出篩選條件。空字串一律視為「不篩」，日期時間
    格式錯誤直接丟 BookingError 讓呼叫端回 400，不要默默忽略——
    使用者明明篩了卻沒有效果，比直接報錯難查得多。"""
    f = {}
    for key in ("mmg_name", "player_name", "role_name", "status"):
        v = (args.get(key) or "").strip()
        if v:
            f[key] = v

    raw_date = (args.get("session_date") or "").strip()
    if raw_date:
        try:
            f["session_date"] = date.fromisoformat(raw_date)
        except ValueError:
            raise BookingError(f"日期格式錯誤：{raw_date}（需要 YYYY-MM-DD）") from None

    raw_time = (args.get("session_time") or "").strip()
    if raw_time:
        try:
            f["session_time"] = time.fromisoformat(
                raw_time if len(raw_time) > 5 else raw_time + ":00"
            )
        except ValueError:
            raise BookingError(f"時間格式錯誤：{raw_time}（需要 HH:MM）") from None

    if f.get("status") and f["status"] not in bookings_db.CONFIRMED_TAB_STATUSES:
        raise BookingError(f"未知的狀態篩選值「{f['status']}」")
    return f


@app.get("/api/gm/dashboard")
@require_role(2)
def gm_dashboard_route():
    """主持人介面初次載入：三個頁籤各第一頁，一次拿完。

    翻頁與篩選都不走這支——那兩件事只影響當前頁籤，走 /api/gm/tab/<tab>
    單獨拿，另外兩個頁籤維持畫面上既有的內容、不重查。
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            return jsonify(bookings_db.gm_dashboard(cur, g.user["id"]))


@app.get("/api/gm/tab/<tab>")
@require_role(2)
def gm_tab_route(tab: str):
    """單一頁籤的一頁資料。翻頁（start）與篩選都在這裡。

    start 是 1-based 起始筆數（1、51、101...）。換篩選條件時前端要把
    start 重設回 1——舊頁碼在新條件下可能根本不存在。
    """
    try:
        start = int(request.args.get("start", 1))
    except ValueError:
        return bad_request("start 必須是數字")
    if start < 1:
        return bad_request("start 必須大於 0")

    try:
        filters = _parse_filters(request.args)
        with pool.connection() as conn:
            with conn.cursor() as cur:
                return jsonify(
                    bookings_db.gm_tab(cur, g.user["id"], tab, start=start, filters=filters)
                )
    except BookingError as exc:
        return jsonify({"error": str(exc)}), 400


# ── 指定行事曆 ──────────────────────────────────────────────────
#
# 形狀跟玩家的預訂行事曆一模一樣（月份給點、日期給清單），差別只有兩個：
# 看的是「指定到我的場次」，而且分類用主持人視角的 category——一場還在等
# **我**確認的戲，整筆的狀態文字會寫成「等待其他主持確認」，那句話在這裡
# 是錯的（見 db/bookings.py 的 _GM_CATEGORY_SQL）。


@app.get("/api/gm/month/<ym>")
@require_role(2)
def gm_month_route(ym: str):
    """這個月每一天指定到我的場次分類，依開始時間排序，不含已取消。"""
    try:
        first = date.fromisoformat(f"{ym}-01")
    except ValueError:
        return bad_request("月份格式錯誤，需要 YYYY-MM")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            days = bookings_db.gm_month_days(cur, g.user["id"], first.year, first.month)
    return jsonify({"month": ym, "days": days})


@app.get("/api/gm/date/<d>")
@require_role(2)
def gm_date_route(d: str):
    """那一天指定到我的場次，含已取消。"""
    try:
        session_date = date.fromisoformat(d)
    except ValueError:
        return bad_request("日期格式錯誤，需要 YYYY-MM-DD")
    try:
        start = int(request.args.get("start", 1))
    except ValueError:
        return bad_request("start 必須是數字")
    if start < 1:
        return bad_request("start 必須大於 0")

    with pool.connection() as conn:
        with conn.cursor() as cur:
            return jsonify(bookings_db.list_gm_date(
                cur, g.user["id"], session_date, start=start,
            ))


# ── 忙碌日 ──────────────────────────────────────────────────────
#
# 兩種來源、兩組端點，因為權限與對象不同：
#   店家忙碌日  管理員設，對象是整家店（/api/admin/busy）
#   主持人忙碌日 主持人設自己的（/api/gm/busy），管理員不能替他設
#
# 擋在哪、為什麼一個可以放行一個不行，見 db/busy.py 的開頭。


def _parse_busy_date(raw: str):
    try:
        return date.fromisoformat(raw)
    except (ValueError, TypeError):
        raise BookingError(f"日期格式錯誤：{raw}（需要 YYYY-MM-DD）") from None


@app.get("/api/admin/busy/<ym>")
@require_role(1)
def admin_busy_month(ym: str):
    """這個月的店家忙碌日，給場次行事曆把格子塗灰。"""
    try:
        first = date.fromisoformat(f"{ym}-01")
    except ValueError:
        return bad_request("月份格式錯誤，需要 YYYY-MM")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            dates = busy_db.dates_in(cur, busy_db.STORE, None, first.year, first.month)
    return jsonify({"month": ym, "dates": dates})


@app.get("/api/admin/busy/check")
@require_role(1)
def admin_busy_check():
    """某一天是不是忙碌日、指定的主持人裡有誰那天忙碌。

    給場次管理的編輯畫面用：管理員選了日期與主持人的當下就要知道，而不是
    按下儲存才被退回。兩種忙碌日的處置不同（見 db/busy.py）：
      store_busy  可以放行，但要先跳對話框確認
      busy_gms    **硬擋**，畫面上先紅字，硬按就跳對話框說明並且不讓存

    ★ 這支是**提醒**，不是把關。真正擋下寫入的是 create_booking 與
      bookings_admin.update——它們各自會再問一次。前端少問一次只會讓錯誤
      晚一點出現，不會讓不該成立的東西成立。
    """
    raw = (request.args.get("date") or "").strip()
    try:
        d = _parse_busy_date(raw)
    except BookingError as exc:
        return jsonify({"error": str(exc)}), 400
    gm_ids = []
    for part in (request.args.get("gm") or "").split(","):
        part = part.strip()
        if part.isdigit():
            gm_ids.append(int(part))

    with pool.connection() as conn:
        with conn.cursor() as cur:
            store = busy_db.store_busy(cur, d)
            ids = busy_db.busy_gm_ids(cur, gm_ids, d)
            names = {}
            if ids:
                cur.execute("SELECT id, name FROM users WHERE id = ANY(%s)", (ids,))
                names = dict(cur.fetchall())
    return jsonify({
        "date": d.isoformat(),
        "store_busy": store,
        "busy_gms": [{"id": i, "name": names.get(i) or f"id={i}"} for i in ids],
    })


@app.post("/api/admin/busy")
@require_role(1)
def admin_busy_set():
    """把某一天設成店家忙碌日。

    ★ 那天已經有場次也照設（案主 2026-09-17：跳警告，他堅持就讓他設）。
      警告由畫面負責——它手上已經有那個月每天有幾場。後端負責的是**寄信**
      通知所有管理員，那是店裡其他人唯一會知道這件事的管道。
    """
    try:
        d = _parse_busy_date((request.get_json(silent=True) or {}).get("date"))
    except BookingError as exc:
        return jsonify({"error": str(exc)}), 400

    with pool.connection() as conn:
        with conn.cursor() as cur:
            added = busy_db.add(cur, busy_db.STORE, None, d)
            outbox = []
            if added:
                sessions = busy_db.sessions_on(cur, d)
                if sessions:
                    outbox = [notify_events.on_busy_day_set(
                        cur, d, sessions, actor_id=g.user["id"])]
            conn.commit()
    # 忙碌日會改變「哪些時段訂得到」，而 /slot 的回應有快取。
    slot_cache.clear()
    notify_events.send(outbox)
    return jsonify({"date": d.isoformat(), "added": added})


@app.delete("/api/admin/busy/<d>")
@require_role(1)
def admin_busy_clear(d: str):
    try:
        day = _parse_busy_date(d)
    except BookingError as exc:
        return jsonify({"error": str(exc)}), 400
    with pool.connection() as conn:
        with conn.cursor() as cur:
            removed = busy_db.remove(cur, busy_db.STORE, None, day)
            conn.commit()
    slot_cache.clear()
    return jsonify({"date": day.isoformat(), "removed": removed})


@app.get("/api/gm/busy/<ym>")
@require_role(2)
def gm_busy_month(ym: str):
    """這個月**自己的**忙碌日。看不到別人的——那不是他的事。"""
    try:
        first = date.fromisoformat(f"{ym}-01")
    except ValueError:
        return bad_request("月份格式錯誤，需要 YYYY-MM")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            dates = busy_db.dates_in(
                cur, busy_db.GM, g.user["id"], first.year, first.month)
    return jsonify({"month": ym, "dates": dates})


@app.post("/api/gm/busy")
@require_role(2)
def gm_busy_set():
    """主持人把某一天標成自己的忙碌日。

    ★ 只能設自己的：user_id 一律取 g.user，不收參數。管理員要替主持人設
      是另一件事（目前不做）——那會讓「誰標的」變得不確定，而這個標記的
      意義正是「這個人自己說那天不行」。
    """
    try:
        d = _parse_busy_date((request.get_json(silent=True) or {}).get("date"))
    except BookingError as exc:
        return jsonify({"error": str(exc)}), 400
    with pool.connection() as conn:
        with conn.cursor() as cur:
            added = busy_db.add(cur, busy_db.GM, g.user["id"], d)
            conn.commit()
    return jsonify({"date": d.isoformat(), "added": added})


@app.delete("/api/gm/busy/<d>")
@require_role(2)
def gm_busy_clear(d: str):
    try:
        day = _parse_busy_date(d)
    except BookingError as exc:
        return jsonify({"error": str(exc)}), 400
    with pool.connection() as conn:
        with conn.cursor() as cur:
            removed = busy_db.remove(cur, busy_db.GM, g.user["id"], day)
            conn.commit()
    return jsonify({"date": day.isoformat(), "removed": removed})


@app.get("/api/bookings/<int:booking_id>/detail")
@require_auth
def booking_detail_route(booking_id: int):
    """某一筆預約的細節：預訂者的聯絡方式，以及這場所有角色與主持人。

    兩個對話框的內容合成一支：分開做會變成點兩個地方各打一次 API，
    但兩者都是「這一筆預約的細節」，一次拿完比較合理。

    誰讀得到（三種，缺一不可地各有理由）：
      管理員　　　　　　全部。經營者要處理任何一筆。
      被指定的主持人　　自己被指定的那幾場。
      這筆預約的預訂者　自己那一筆。

    ★ 最後一條是 2026-08-31 加的，讓玩家在「我預訂的場次」能看到自己
      這場的主持人是誰、確認了沒。安全上成立的理由是：這支回傳的個資
      就是**他自己的**姓名與聯絡方式，而主持人名單本來就是他預約時
      自己選的。他讀不到任何別人的東西。

    ★ 守衛從 require_role(2) 換成 require_auth，判斷改在函式內做——
      因為現在的規則不是「某個等級以上」，而是「管理員、或跟這一筆有
      關係的人」。用 require_role 表達不出後者。
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.gm1_user_id, b.gm2_user_id, b.gm3_user_id, b.gm4_user_id,
                       b.gm1_confirmed, b.gm2_confirmed, b.gm3_confirmed, b.gm4_confirmed,
                       m.gm1_name, m.gm2_name, m.gm3_name, m.gm4_name,
                       u.name, u.email, u.line_id, u.phone,
                       m.name, b.session_date, b.session_time, b.status, b.note,
                       b.player_id
                  FROM booking_record b
                  JOIN mmg m   ON m.id = b.mmg_id
                  JOIN users u ON u.id = b.player_id
                 WHERE b.id = %s
                """,
                (booking_id,),
            )
            row = cur.fetchone()
            if row is None:
                return jsonify({"error": "找不到這筆預約"}), 404

            gm_ids = list(row[0:4])
            allowed = (
                g.user["role"] <= 1                    # 管理員
                or g.user["id"] in gm_ids            # 被指定的主持人
                or g.user["id"] == row[21]             # 這筆預約的預訂者
            )
            if not allowed:
                return jsonify({"error": "只能查看自己的場次"}), 403

            # 把有指派的主持人名字一次查出來，避免四次單筆查詢
            present = [h for h in gm_ids if h is not None]
            names = {}
            if present:
                cur.execute("SELECT id, name FROM users WHERE id = ANY(%s)", (present,))
                names = dict(cur.fetchall())

    confirmed = list(row[4:8])
    role_names = list(row[8:12])
    return jsonify({
        "id": booking_id,
        "mmg_name": row[16],
        "session_date": row[17].isoformat(),
        "session_time": row[18].strftime("%H:%M"),
        "status": row[19],
        "status_label": bookings_db.STATUS_LABEL.get(row[19], row[19]),
        "note": row[20],
        "player": {
            "name": row[12], "email": row[13],
            "line_id": row[14], "phone": row[15],
        },
        # 只回傳這齣戲真的有的角色（gm{n}_name 不是 NULL 的）
        "gm_slots": [
            {
                "slot": i + 1,
                "role_name": role_names[i],
                "user_id": gm_ids[i],
                "user_name": names.get(gm_ids[i]),
                "confirmed": confirmed[i],
            }
            for i in range(4)
            if role_names[i] is not None
        ],
    })


@app.get("/api/bookings/<int:booking_id>/conflicts")
@require_role(2)
def booking_conflicts_route(booking_id: int):
    """這筆預約的主持人，有沒有人已經被別的已成立場次佔住。

    純查詢，不寫入也不擋任何動作。主持人按確認、管理員切狀態之前各呼叫
    一次，撞期就跳對話框讓人自己決定要不要繼續——這兩個畫面的用途就是
    人工判斷，系統該做的是把資訊攤開，不是替他決定。

    回傳兩組（見 find_conflicts）：script 是這個劇本被排了兩場，gms 是
    某位主持人同時段已經有別的場。兩者的嚴重程度不同——劇本撞期是排班
    問題，人可以判斷後放行；主持人自己撞期是他分身乏術，不該放行——所以
    分開給，不合併成一份清單。

    gms 的每一筆帶 is_me，標出是不是呼叫者自己。主持人只在意自己撞到
    什麼。在這裡標而不是讓前端比對，是因為前端拿不到自己的 user id
    （登入回應只給 email/name/role），為了這件事改動登入回應的形狀不
    划算，而後端本來就知道自己是誰。

    ★ 跟 /detail 一樣限定「自己被指定的場」。這支回傳的是別人的場次
      名稱、日期時間，以及哪幾位主持人被佔住——把 id 換個數字就能一筆
      一筆問出全店的排班。畫面只會帶主持人到自己的場，但畫面帶不到的
      地方不代表到不了。
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            booking = bookings_db.get_booking(cur, booking_id)
            if booking is None:
                return jsonify({"error": "找不到這筆預約"}), 404
            if g.user["role"] > 1 and not _is_assigned_gm(booking, g.user["id"]):
                return jsonify({"error": "只能查看自己被指定的場次"}), 403
            result = bookings_db.find_conflicts(cur, booking_id)
    for c in result["gms"]:
        c["is_me"] = c["gm_id"] == g.user["id"]
    return jsonify(result)


@app.post("/api/bookings/<int:booking_id>/confirm")
@require_role(2)
def confirm_booking_route(booking_id: int):
    """主持人確認自己在這場的指定。

    刻意不檢查「這個人是不是這場的主持人」——confirm_gm() 的 WHERE
    已經包含那個條件，不符合就拋 BookingError。在這裡多做一次檢查，
    等於同一條規則有兩個地方可以改，遲早會不一致。
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            before = bookings_db.get_booking(cur, booking_id)
            try:
                updated = bookings_db.confirm_gm(cur, booking_id, g.user["id"])
            except BookingError as exc:
                conn.rollback()
                return jsonify({"error": str(exc)}), 409
            outbox = notify_events.on_booking_change(cur, before, updated, actor_id=g.user["id"])
            conn.commit()
    notify_events.send(outbox)
    return jsonify(updated)


# ── 管理員介面 ──────────────────────────────────────────────────
#
# 全部 require_role(1)：只有管理員。跟主持人介面不同，這些路由看的是
# 全站資料，不是「呼叫者自己被指定的」。

def _admin_write(fn):
    """管理員寫入路由共用的殼：開連線、跑、成功就 commit、
    BookingError 回 400 並 rollback，最後送出 g.outbox 裡的通知（信與 LINE 推播）。"""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        body = request.get_json(silent=True) or {}
        g.outbox = []
        g.after_commit = []
        with pool.connection() as conn:
            with conn.cursor() as cur:
                try:
                    result = fn(cur, body, *args, **kwargs)
                except BookingError as exc:
                    conn.rollback()
                    return jsonify({"error": str(exc)}), 400
                conn.commit()
        # commit 之後才做的事（例如清快取）。在 commit 前清的話，中間那一瞬間
        # 進來的讀取會把舊資料重新放回快取。
        for after in g.after_commit:
            after()
        # ★ commit 成功才送。被包住的路由把要發的通知放進 g.outbox，
        #   由這裡統一在交易外送出——寄信、推播都不該有機會影響交易。
        notify_events.send(g.outbox)
        return result
    return wrapper


@app.post("/api/admin/bookings/tidy")
@require_role(1)
def admin_tidy_bookings():
    """管理員手動的「場次整理」。

    跟每日排程做同一件事，差別只在時間範圍：這支不設回看窗口，把所有
    今天以前的場次一次處理完。排程漏跑幾天之後用這個補，不必為了防漏
    再疊一層自動補償機制。

    跟排程那支分開路由而不是加個參數：這支的授權方式不同（管理員登入
    session，不是 JOB_TOKEN），混在一起會讓「誰能觸發什麼」變得不明顯。
    """
    result = jobs.run_full(pool)
    return jsonify(result), (500 if result.get("errors") else 200)


@app.get("/api/admin/abuse")
@require_role(1)
def admin_abuse_log():
    """異常請求紀錄。不帶 user_id 就是全站最近的。

    這張表回答的是「誰在什麼時候打了什麼、幾次」——只給一個累計數字的話，
    看到 47 也不知道該不該停權。
    """
    raw = request.args.get("user_id")
    try:
        user_id = int(raw) if raw else None
        start = int(request.args.get("start", 1))
    except ValueError:
        return bad_request("user_id 與 start 必須是數字")
    if start < 1:
        return bad_request("start 必須大於 0")

    with pool.connection() as conn:
        with conn.cursor() as cur:
            data = guard.list_log(cur, user_id=user_id, start=start)
            cur.execute("SELECT EXISTS (SELECT 1 FROM abuse_rule)")
            auto_deactivate = bool(cur.fetchone()[0])
    return jsonify({
        **data,
        # 讓管理員知道現在是「只記錄」還是「會自動停權」，不必去翻程式碼。
        #
        # ★ 這裡原本讀 guard.AUTO_DEACTIVATE——一個布林常數。8265a2c 把停權
        #   改成「累計點數對照 abuse_rule 表」之後那個常數就沒了，但這一行
        #   留著，於是這支從那次起**每一次都回 500**。沒有人發現是因為前端
        #   把失敗畫成了「目前沒有異常紀錄」，看起來就只是還沒有人觸發過。
        #
        #   現在改成問資料：abuse_rule 一筆都沒有時 _apply_points() 找不到
        #   規則、不會停權，那才是真正的「只記錄」。
        "auto_deactivate": auto_deactivate,
        "rate_limit_per_minute": guard.RATE_LIMIT_PER_MINUTE,
    })


@app.delete("/api/admin/abuse")
@require_role(1)
def admin_clear_abuse_log():
    """清除紀錄。帶 user_id 就只清那個人的。

    可以刪是刻意的——見 auth_utils/guard.py 的 clear_log()。
    """
    raw = request.args.get("user_id")
    try:
        user_id = int(raw) if raw else None
    except ValueError:
        return bad_request("user_id 必須是數字")

    with pool.connection() as conn:
        with conn.cursor() as cur:
            removed = guard.clear_log(cur, user_id=user_id)
            conn.commit()
    if user_id:
        guard.reset_rate(user_id)
    return jsonify({"removed": removed})


@app.get("/api/admin/mmg")
@require_role(1)
def admin_list_mmg():
    """全部劇本，含停用的。同時附上可指派的主持人名單，讓前端的
    「新增主持人」下拉選單不用另外打一支 API。"""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # 順帶把到點的上下架落地。管理員的劇本清單是低頻查詢，而且
            # 正是「狀態」這一欄被看到的地方——在這裡補，管理員看到的
            # 就一定是對的，而不必為此養一個每分鐘醒來的排程
            #（那會讓 Neon 的 compute 永遠睡不著，見 db/listing.py）。
            listing.sync_status(cur)
            conn.commit()
            items = mmg_admin.list_all(cur)
            # 排期設定兩個查詢一次撈完再貼回去，不是每齣戲各查一次。
            rules = schedule.list_all_rules(cur)
            for item in items:
                item["schedule"] = rules.get(
                    item["id"], {"weekly": [], "exceptions": []}
                )
            return jsonify({
                "items": items,
                "gm_candidates": mmg_admin.list_gm_candidates(cur),
                "rooms": mmg_admin.list_rooms(cur),
            })


@app.put("/api/admin/mmg/<int:mmg_id>")
@require_role(1)
@_admin_write
def admin_update_mmg(cur, body, mmg_id: int):
    # 名稱、價錢、上下架都會出現在公開的簡介頁上（見 intro_public.invalidate）。
    # commit 之後才清（_admin_write 的 after_commit），理由見那裡。
    g.after_commit.append(intro_public.invalidate)
    changed = mmg_admin.update(cur, mmg_id, body)
    changed = _save_schedule(cur, mmg_id, body) or changed
    # ★ 一定排在兩個寫入都做完之後：價格與排期是同一次存檔，要看的是
    #   合併後的結果（理由見 check_deposit_coverage）。擋下來的話
    #   _admin_write 會把整個交易 rollback，兩邊都不會寫進去。
    bookings_db.check_deposit_coverage(cur, mmg_id)
    return jsonify({"changed": changed, "item": _mmg_with_schedule(cur, mmg_id)})


@app.post("/api/admin/mmg")
@require_role(1)
@_admin_write
def admin_create_mmg(cur, body):
    g.after_commit.append(intro_public.invalidate)   # 同上：「找不到這齣」的結果也有快取
    new_id = mmg_admin.create(cur, body)
    _save_schedule(cur, new_id, body)
    bookings_db.check_deposit_coverage(cur, new_id)  # 同上
    return jsonify(_mmg_with_schedule(cur, new_id)), 201


# ── 劇本簡介頁（公開，不用登入）──────────────────────────────────
#
# 內容與規則見 db/intro.py，防攻擊見 intro_public.py。前端網址是
# https://<店家前端>/mmg/<id>（Cloudflare 的 _redirects 把它導到首頁，由前端畫）。


def _rate_limited(kind: str):
    """公開簡介頁的限流。通過回 (None, ip, 原始標頭)，被擋回 (429 回應, ...)。"""
    ip, chain = intro_public.client_ip(request)
    retry = intro_public.check_rate(ip, kind)
    if retry is None:
        return None, ip, chain
    resp = jsonify({"error": "請求過於頻繁，請稍候再試"})
    resp.headers["Retry-After"] = str(retry)
    return (resp, 429), ip, chain


def _db_budget_exhausted():
    """今天的資料庫查詢額度用完了就回 503（見 intro_public 的第 5 點），否則 None。"""
    retry = intro_public.take_db_budget()
    if retry is None:
        return None
    resp = jsonify({"error": "簡介頁今天的讀取量已達上限，請明天再試"})
    resp.headers["Retry-After"] = str(retry)
    return resp, 503


def _flush_intro_views() -> None:
    """把暫存的瀏覽紀錄寫進資料庫。失敗就放回去，下一次再寫——紀錄不該讓
    看簡介的人看到錯誤。"""
    rows = intro_public.take_views()
    if not rows:
        return
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                intro_db.insert_views(cur, rows)
                conn.commit()
    except Exception as exc:  # noqa: BLE001 - 觀測用的旁支
        intro_public.give_back(rows)
        app.logger.warning("簡介頁瀏覽紀錄寫入失敗：%s", exc)


@app.get("/api/public/mmg/<int:mmg_id>")
def public_intro(mmg_id: int):
    """簡介頁的內容。不用登入，只回上架中的劇本。

    ★ 快取命中就完全不碰資料庫（見 intro_public.py 開頭：公開頁面最大的風險是
      被拿來叫醒資料庫）。找不到的結果也快取，亂猜編號一樣打不到資料庫。
    """
    limited, ip, chain = _rate_limited("intro")
    if limited:
        return limited
    payload = intro_public.cached_intro(mmg_id)
    if payload is intro_public.MISS:
        busy = _db_budget_exhausted()
        if busy:
            return busy
        with pool.connection() as conn:
            with conn.cursor() as cur:
                payload = intro_db.get_public(cur, mmg_id)
                # 同一條連線把這一齣的圖全部撈進快取（intro_public 第 2 點）：
                # 接下來瀏覽器要的封面與角色圖就不必再各查一次資料庫。
                images = intro_db.public_images(cur, mmg_id) if payload else []
        intro_public.store_intro(mmg_id, payload)
        for key, data in images:
            intro_public.store_image(key, data)
    if payload is None:
        return jsonify({"error": "找不到這個劇本"}), 404
    if intro_public.record_view(mmg_id, ip, chain, request.headers.get("User-Agent", "")):
        _flush_intro_views()
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp


def _public_image(key: tuple, load):
    """公開的圖片（封面、角色圖）。網址帶版本號（換圖就換網址），所以可以快取一年。
    load(cur) 去資料庫拿；快取命中就不連資料庫。"""
    limited, _ip, _chain = _rate_limited("cover")
    if limited:
        return limited
    data = intro_public.cached_image(key)
    if data is intro_public.MISS:
        busy = _db_budget_exhausted()
        if busy:
            return busy
        with pool.connection() as conn:
            with conn.cursor() as cur:
                data = load(cur)
        intro_public.store_image(key, data)
    if data is None:
        return jsonify({"error": "找不到這張圖"}), 404
    resp = app.response_class(data, mimetype="image/webp")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.get("/api/public/mmg/<int:mmg_id>/cover/<int:version>.webp")
def public_intro_cover(mmg_id: int, version: int):
    return _public_image(
        ("cover", mmg_id, version),
        lambda cur: intro_db.get_cover(cur, mmg_id, version, public=True),
    )


@app.get("/api/public/mmg/<int:mmg_id>/characters/<int:character_id>/<int:version>.webp")
def public_intro_character_image(mmg_id: int, character_id: int, version: int):
    return _public_image(
        ("character", mmg_id, character_id, version),
        lambda cur: intro_db.get_character_image(cur, mmg_id, character_id, version, public=True),
    )


@app.get("/api/admin/mmg/<int:mmg_id>/intro")
@require_role(1)
def admin_get_intro(mmg_id: int):
    """編輯頁用：不管上不上架都給，另外附上 public（現在公開了沒有）。"""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            item = intro_db.get_for_admin(cur, mmg_id)
    if item is None:
        return jsonify({"error": "找不到這個劇本"}), 404
    return jsonify(item)


@app.put("/api/admin/mmg/<int:mmg_id>/intro")
@require_role(1)
def admin_update_intro(mmg_id: int):
    """存劇本簡介、角色、老闆的話、類型標籤（整份覆蓋）。"""
    body = request.get_json(silent=True) or {}
    with pool.connection() as conn:
        with conn.cursor() as cur:
            try:
                found = intro_db.update_text(cur, mmg_id, body)
            except intro_db.IntroError as exc:
                conn.rollback()
                return jsonify({"error": str(exc)}), 400
            if not found:
                return jsonify({"error": "找不到這個劇本"}), 404
            item = intro_db.get_for_admin(cur, mmg_id)
            conn.commit()
    intro_public.invalidate()
    return jsonify(item)


@app.put("/api/admin/mmg/<int:mmg_id>/cover")
@require_role(1)
def admin_upload_cover(mmg_id: int):
    """上傳封面圖：請求本體就是圖片檔。轉成 WebP 存（見 admin_utils/cover_image.py）。

    ★ 全站的請求大小上限是 64KB（見檔案開頭的 MAX_CONTENT_LENGTH），只有這一支
      放寬。放寬要在讀本體**之前**設，讀了就來不及了。
    """
    request.max_content_length = cover_image.MAX_UPLOAD_BYTES
    try:
        webp = cover_image.to_webp(request.get_data())
    except cover_image.CoverError as exc:
        return jsonify({"error": str(exc)}), 400
    with pool.connection() as conn:
        with conn.cursor() as cur:
            version = intro_db.set_cover(cur, mmg_id, webp)
            if version is None:
                return jsonify({"error": "找不到這個劇本"}), 404
            item = intro_db.get_for_admin(cur, mmg_id)
            conn.commit()
    intro_public.invalidate()
    return jsonify(item)


@app.delete("/api/admin/mmg/<int:mmg_id>/cover")
@require_role(1)
def admin_delete_cover(mmg_id: int):
    with pool.connection() as conn:
        with conn.cursor() as cur:
            if intro_db.set_cover(cur, mmg_id, None) is None:
                return jsonify({"error": "找不到這個劇本"}), 404
            item = intro_db.get_for_admin(cur, mmg_id)
            conn.commit()
    intro_public.invalidate()
    return jsonify(item)


@app.post("/api/admin/mmg/<int:mmg_id>/characters")
@require_role(1)
def admin_create_character(mmg_id: int):
    """單獨新增一個角色（見 db/intro.create_character）。回 {"id": 新角色, "intro": 整份簡介}。

    body：{"name", "description", "position"}，position 是它在畫面上是第幾個（1 起算），
    只用在錯誤訊息。
    """
    body = request.get_json(silent=True) or {}
    n = body.get("position") if isinstance(body.get("position"), int) else 1
    with pool.connection() as conn:
        with conn.cursor() as cur:
            try:
                cid = intro_db.create_character(cur, mmg_id, body, n)
            except intro_db.IntroError as exc:
                conn.rollback()
                return jsonify({"error": str(exc)}), 400
            if cid is None:
                return jsonify({"error": "找不到這個劇本"}), 404
            item = intro_db.get_for_admin(cur, mmg_id)
            conn.commit()
    intro_public.invalidate()
    return jsonify({"id": cid, "intro": item})


@app.put("/api/admin/mmg/<int:mmg_id>/characters/<int:character_id>/image")
@require_role(1)
def admin_upload_character_image(mmg_id: int, character_id: int):
    """上傳角色圖：做法同封面（admin_upload_cover），只是縮得比較小。

    角色要先存在（有 id）才能放圖：還沒存過的角色，前端先用 admin_create_character
    單獨建出來再傳圖。
    """
    request.max_content_length = cover_image.MAX_UPLOAD_BYTES
    try:
        webp = cover_image.to_webp(request.get_data(), cover_image.CHARACTER_MAX)
    except cover_image.CoverError as exc:
        return jsonify({"error": str(exc)}), 400
    return _set_character_image(mmg_id, character_id, webp)


@app.delete("/api/admin/mmg/<int:mmg_id>/characters/<int:character_id>/image")
@require_role(1)
def admin_delete_character_image(mmg_id: int, character_id: int):
    return _set_character_image(mmg_id, character_id, None)


def _set_character_image(mmg_id: int, character_id: int, webp: bytes | None):
    with pool.connection() as conn:
        with conn.cursor() as cur:
            if not intro_db.set_character_image(cur, mmg_id, character_id, webp):
                return jsonify({"error": "找不到這個角色"}), 404
            item = intro_db.get_for_admin(cur, mmg_id)
            conn.commit()
    intro_public.invalidate()
    return jsonify(item)


@app.get("/api/admin/mmg/<int:mmg_id>/characters/<int:character_id>/<int:version>.webp")
@require_role(1)
def admin_intro_character_image(mmg_id: int, character_id: int, version: int):
    """編輯頁預覽用（同 admin_intro_cover）。"""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            data = intro_db.get_character_image(cur, mmg_id, character_id, version, public=False)
    if data is None:
        return jsonify({"error": "找不到這張圖"}), 404
    resp = app.response_class(data, mimetype="image/webp")
    resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return resp


@app.get("/api/admin/mmg/<int:mmg_id>/cover/<int:version>.webp")
@require_role(1)
def admin_intro_cover(mmg_id: int, version: int):
    """編輯頁預覽用：不管上不上架都給。前端用帶登入憑證的 fetch 拿，不是 <img src>。"""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            data = intro_db.get_cover(cur, mmg_id, version, public=False)
    if data is None:
        return jsonify({"error": "找不到這張圖"}), 404
    resp = app.response_class(data, mimetype="image/webp")
    resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return resp


def _save_schedule(cur, mmg_id: int, body: dict) -> bool:
    """寫入排期規則，回傳有沒有真的改到。

    ★ 跟劇本其他欄位在同一個交易裡寫，理由跟主持人名單一樣：畫面上是
      「改完一起按儲存」。拆成兩支 API 的話，第二支失敗時會留下一齣
      基本資料已經改好、排期卻還是舊的劇本，而使用者看到的是一句
      「儲存失敗」——他無從得知有一半已經寫進去了。

    body 沒有 schedule 這個鍵時什麼都不做。這讓還沒更新的舊前端、
    以及只想改個價格的呼叫端，不會意外把排期規則清空。
    """
    rules = body.get("schedule")
    if rules is None:
        return False
    before = schedule.get_rules(cur, mmg_id)
    try:
        schedule.set_rules(
            cur, mmg_id, rules.get("weekly"), rules.get("exceptions")
        )
    except schedule.ScheduleError as exc:
        raise BookingError(str(exc)) from None
    return schedule.get_rules(cur, mmg_id) != before


def _mmg_with_schedule(cur, mmg_id: int) -> dict:
    item = mmg_admin.get(cur, mmg_id)
    if item is not None:
        item["schedule"] = schedule.get_rules(cur, mmg_id)
    return item


@app.get("/api/admin/users")
@require_role(1)
def admin_list_users():
    """使用者清單，順便把角色清單一起回。

    ★ 角色跟著這一支回，而不是另開一支端點：前端的編輯畫面需要「有哪些
      角色可以選」，而它本來就會呼叫這一支。分成兩支的話多一次往返，而且
      清單顯示的名稱與下拉選項的名稱有機會來自不同時間點的資料——同一個
      畫面上同一個角色顯示兩種字，那比不做還糟。
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            return jsonify({
                "items": users_admin.list_all(cur),
                "roles": [
                    {"value": r["id"], "label": r["name"]}
                    for r in roles.all_roles(cur).values()
                ],
            })


@app.put("/api/admin/users/<int:user_id>")
@require_role(1)
@_admin_write
def admin_update_user(cur, body, user_id: int):
    changed = users_admin.update(cur, user_id, body)
    return jsonify({"changed": changed, "item": users_admin.get(cur, user_id)})


@app.post("/api/admin/users")
@require_role(1)
@_admin_write
def admin_create_user(cur, body):
    new_id = users_admin.create(cur, body)
    return jsonify(users_admin.get(cur, new_id)), 201


@app.get("/api/admin/bookings")
@require_role(1)
def admin_bookings_overview():
    """初次載入：這家店到得了的每一個子頁籤各第一頁（不是固定五個，
    見 db/bookings.reachable_tabs）。翻頁與篩選走 /tab/<tab>。"""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            return jsonify({
                "tabs": bookings_db.reachable_tabs(cur),
                "data": bookings_admin.overview(cur),
            })


@app.get("/api/admin/bookings/tab/<tab>")
@require_role(1)
def admin_bookings_tab(tab: str):
    try:
        start = int(request.args.get("start", 1))
    except ValueError:
        return bad_request("start 必須是數字")
    if start < 1:
        return bad_request("start 必須大於 0")

    try:
        # status 由子頁籤決定，篩選器沒有這一項（見 bookings_admin）
        filters = _parse_filters(request.args)
        filters.pop("status", None)
        with pool.connection() as conn:
            with conn.cursor() as cur:
                return jsonify(
                    bookings_admin.list_tab(cur, tab, start=start, filters=filters)
                )
    except BookingError as exc:
        return jsonify({"error": str(exc)}), 400


# ── 場次行事曆 ──────────────────────────────────────────────────
#
# 跟另外兩份行事曆同一個形狀（月份給點、日期給清單），這一份是全站視角：
# 只要是非取消的場次都算，不分是誰訂的、誰主持。


@app.get("/api/admin/bookings/month/<ym>")
@require_role(1)
def admin_bookings_month(ym: str):
    """全站這個月每一天有哪些場次的狀態，依開始時間排序，不含已取消。"""
    try:
        first = date.fromisoformat(f"{ym}-01")
    except ValueError:
        return bad_request("月份格式錯誤，需要 YYYY-MM")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            days = bookings_admin.month_days(cur, first.year, first.month)
    return jsonify({"month": ym, "days": days})


@app.get("/api/admin/bookings/date/<d>")
@require_role(1)
def admin_bookings_date(d: str):
    """全站那一天的場次，含已取消。形狀與子頁籤清單完全相同。"""
    try:
        session_date = date.fromisoformat(d)
    except ValueError:
        return bad_request("日期格式錯誤，需要 YYYY-MM-DD")
    try:
        start = int(request.args.get("start", 1))
    except ValueError:
        return bad_request("start 必須是數字")
    if start < 1:
        return bad_request("start 必須大於 0")

    with pool.connection() as conn:
        with conn.cursor() as cur:
            return jsonify(bookings_admin.list_date(cur, session_date, start=start))


@app.post("/api/admin/bookings")
@require_role(1)
@_admin_write
def admin_create_booking(cur, body):
    """管理員手動建立場次。日期時間在這裡就解析成 date/time 物件，
    底下的 create/update 都以物件比對，不處理字串。"""
    d, t = _parse_slot(body)
    body = {**body, "session_date": d, "session_time": t}
    new_id = bookings_admin.create(cur, body)
    created = bookings_db.get_booking(cur, new_id)
    g.outbox = notify_events.on_booking_change(cur, None, created, actor_id=g.user["id"])
    # 忙碌日被排進場次：⑧寄所有管理員、⑨寄被指定的主持人。這一支自己會問
    # 「那天是不是忙碌日」，不是忙碌日就回空清單。
    g.outbox += notify_events.on_busy_day_booking(
        cur, None, created, actor_id=g.user["id"]) or []
    return jsonify(created), 201


@app.put("/api/admin/bookings/<int:booking_id>")
@require_role(1)
@_admin_write
def admin_update_booking(cur, body, booking_id: int):
    data = dict(body)
    # 日期時間從 JSON 進來是字串，轉成 date/time 才能跟資料庫的值比對
    if data.get("session_date") or data.get("session_time"):
        try:
            d, t = _parse_slot(data)
            data["session_date"], data["session_time"] = d, t
        except BookingError:
            raise
    before = bookings_db.get_booking(cur, booking_id)
    changed = bookings_admin.update(cur, booking_id, data)
    after = bookings_db.get_booking(cur, booking_id)
    # 後台可以直接把狀態改成 gm_reviewed 或 booked，也可以補指派主持人——
    # 前後比對讓這三種都跟正常流程走同一條通知規則（見 notify_events）。
    g.outbox = notify_events.on_booking_change(cur, before, after, actor_id=g.user["id"])
    # 改期到忙碌日也算「被排進去」（案主 2026-09-18）。只改備註、改訂金
    # 不會觸發——判斷在 on_busy_day_booking 裡。
    g.outbox += notify_events.on_busy_day_booking(
        cur, before, after, actor_id=g.user["id"]) or []
    return jsonify({"changed": changed, "item": after})


if __name__ == "__main__":
    # 預設 5001 不是 5000：macOS 的 AirPlay 接收器佔用 5000，
    # 會回 403 而不是連線被拒，症狀看起來像應用程式壞了。
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)))
