"""
booking-king 後端。

健康檢查、保活端點、Google 登入與 session 層，以及劇本查詢與預約路由。
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from functools import wraps

from dotenv import load_dotenv

# 一定要在 import 任何會讀 os.environ 的模組之前載入。db.connection 在
# import 當下就會讀 DATABASE_URL，晚一步就是 KeyError。
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from flask import Flask, g, jsonify, request  # noqa: E402
from flask_cors import CORS  # noqa: E402

from auth_utils import sessions, whitelist  # noqa: E402
from auth_utils.decorators import require_auth, require_role  # noqa: E402
from auth_utils.google_auth import extract_bearer_token, verify_google_id_token  # noqa: E402
from admin_utils import bookings_admin, mmg_admin, users_admin  # noqa: E402
import jobs  # noqa: E402
from db import bookings as bookings_db  # noqa: E402
from db import mmg as mmg_db  # noqa: E402
from db.bookings import BookingError  # noqa: E402
from db.connection import pool  # noqa: E402
from db.schema import ensure_schema  # noqa: E402

app = Flask(__name__)

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
#     有效——/api/mmg、/api/host/dashboard、POST /api/bookings 這些沒有
#            查詢字串的，以及開賣時大家不斷重整同一個場次
#     無效——瀏覽不同日期的場次、換篩選條件翻頁，每組參數都要各自預檢
#   別以為裝了這行就沒有預檢了。
#
# 瀏覽器對這個值有自己的上限（Chrome 2 小時、Safari 更短），設得再大也
# 會被夾到上限，所以填 1 小時，不是愈大愈好。
CORS(app, max_age=3600)

# Flask 預設會把 JSON 物件的鍵照字母排序輸出。對大部分回應無所謂，但
# 管理員場次管理的 tabs 是「照營運流程排的子頁籤順序」
# （待主持確認 → 待收訂金 → 已成立 → 已結束 → 已取消），排序過會變成
# booked, cancelled, ended... 前端照著畫就是亂的。關掉排序，讓後端
# 定義的順序原樣傳到前端。
app.json.sort_keys = False

# 連線池建立時是 open=False（避免在 import 期就連線，那個時間點 .env 可能
# 還沒載入）。到這裡環境變數已經就緒，可以打開了。
#
# 放在模組層而不是 __main__：正式環境用 gunicorn 之類的 WSGI 伺服器啟動時
# 不會執行 __main__，只放那裡的話線上會是一個永遠沒打開的池子。
#
# min_size=0，所以 open() 本身不會真的建立任何連線，只是把池子標成可用——
# 這裡不會因為 Neon 正在冷啟動而卡住。
pool.open()

with pool.connection() as _conn:
    with _conn.cursor() as _cur:
        ensure_schema(_cur)
        _conn.commit()


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


@app.post("/api/jobs/daily")
def run_daily_jobs():
    """每日排程的觸發點（cron-job.org 每天 00:00 打這一支）。

    用 JOB_TOKEN 保護，不掛 require_role：這支不是由人呼叫的，沒有登入
    session 可用。token 從環境變數讀，沒設就一律拒絕——預設關閉，不是
    預設開放。

    任何一段出錯就回 500，讓排程服務按它自己的設定重試。重跑是安全的
    （見 jobs.py），所以重試不會有副作用。
    """
    expected = os.environ.get("JOB_TOKEN")
    if not expected:
        return jsonify({"error": "JOB_TOKEN 未設定，排程端點停用"}), 503
    if request.headers.get("X-Job-Token") != expected:
        return jsonify({"error": "未授權"}), 401

    result = jobs.run_daily(pool)
    return jsonify(result), (500 if result.get("errors") else 200)


@app.get("/api/ready")
def ready():
    """連資料庫一起檢查。部署後確認整條路徑通了用的，不要拿來當保活目標
    （那會讓每次保活都把 Neon 喚醒，把免費的 CU-hours 額度燒掉）。"""
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return jsonify({"status": "ok", "database": "ok"})
    except Exception as exc:  # noqa: BLE001 - 回報狀態，不讓例外變成 500
        return jsonify({"status": "degraded", "database": str(exc)}), 503


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
            if not user or user["status"] != "active":
                # 帳號不存在或已停用。刻意不自動建立帳號——這個系統的
                # 使用者是由管理員建立的，不是自助註冊。
                return jsonify({"error": "此帳號未獲授權", "email": email}), 403
            token = sessions.create_session(cur, email)
            conn.commit()

    return jsonify(
        {
            "session_token": token,
            "email": user["email"],
            "name": user["name"],
            "role": user["role"],
            "is_admin": user["role"] == 1,
        }
    )


@app.post("/auth/logout")
def logout():
    """登出。沒帶 token 或 token 不存在都視為成功——呼叫端要的結果
    （這張憑證不再有效）本來就已經成立。"""
    token = extract_bearer_token(request)
    if token:
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

    with pool.connection() as conn:
        with conn.cursor() as cur:
            email = sessions.resolve_session(cur, token)
            if email is None:
                conn.commit()
                return jsonify({"error": "登入憑證無效或已過期"}), 401
            user = whitelist.get_user(cur, email)
            conn.commit()

    authorized = bool(user and user["status"] == "active")
    return jsonify(
        {
            "authorized": authorized,
            "email": user["email"] if user else email,
            "name": user["name"] if user else None,
            "role": user["role"] if user else None,
            "is_admin": bool(user and user["role"] == 1),
        }
    )


@app.get("/api/me")
@require_auth
def me():
    """目前登入者的資料。也是驗證 require_auth 有沒有正常運作的最小路由。"""
    return jsonify(g.user)


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


@app.get("/api/mmg/<int:mmg_id>/slot")
@require_auth
def get_slot(mmg_id: int):
    """某個劇本某個時段目前的排隊狀況。

    這是開賣尖峰時最熱的讀取路徑（大量玩家重複整理看還有沒有位子）。
    之後要接 Redis 快取的話就是快取這一支，寫入路徑不動。
    """
    try:
        d, t = _parse_slot(request.args)
    except BookingError as exc:
        return jsonify({"error": str(exc)}), 400

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

    capacity = item["waitlist_limit"]
    # 這個時段有沒有已經成立的那一組。玩家看到的說法差很多：已經有人
    # 成立了，後面排的就是候補；都還沒成立的話，大家只是先後順序不同。
    has_booked = any(st == "booked" for _p, _b, _n, st, _d, _t2, _r in queue)
    return jsonify({
        "mmg_id": mmg_id,
        "mmg_name": item["name"],
        "session_date": d.isoformat(),
        "session_time": t.strftime("%H:%M"),
        "capacity": capacity,
        "taken": len(queue),
        "has_booked": has_booked,
        "is_full": len(queue) >= capacity,
        # ★ 只回一個布林值，不回是哪一場撞到。
        #   玩家不需要知道包廂裡在演什麼、誰訂的、演到幾點——那是別的
        #   客人的預約。前端就算不顯示，送出去的東西打開開發者工具就
        #   看得到，所以不顯示的資訊也不要送。
        #   管理員與主持人要看細節，走 /api/bookings/<id>/conflicts，
        #   那一支有 role <= 2 的把關。
        "has_conflict": clash is not None,
        # ★ 刻意不回排隊清單。玩家需要知道的只有「排了幾組、我會是第幾個、
        #   訂不訂得成」——前面排的是誰、他們的預約在什麼狀態，那是別的
        #   客人的資料，跟他要做的決定無關。
        #   管理員與主持人的畫面各自有完整清單，那兩支都有權限把關。
    })


@app.get("/api/bookings/check-host")
@require_auth
def check_host_route():
    """玩家在預約畫面選了某位主持人時，即時確認他這個時段有沒有空。

    跟送出時的檢查是同一個函式（conflicting_host），只是提早跑一次，
    讓玩家在填表當下就知道，而不是全部填完按下送出才被退回。這裡不做
    任何寫入，也不取任何鎖——真正的把關仍然在 create_booking() 裡，
    那裡才有 advisory lock 保證「檢查到寫入」之間沒有人插隊。
    """
    try:
        mmg_id = int(request.args.get("mmg_id", ""))
        user_id = int(request.args.get("user_id", ""))
    except ValueError:
        return jsonify({"error": "需要 mmg_id 與 user_id"}), 400

    try:
        d, t = _parse_slot(request.args)
    except BookingError as exc:
        return jsonify({"error": str(exc)}), 400

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM mmg WHERE id = %s", (mmg_id,))
            if cur.fetchone() is None:
                return jsonify({"error": "找不到這個劇本"}), 404

            conflicting = bookings_db.conflicting_host(
                cur, [user_id], mmg_id, d, t
            )
            if conflicting is None:
                return jsonify({"ok": True})

            cur.execute("SELECT name FROM users WHERE id = %s", (conflicting,))
            name_row = cur.fetchone()
            host_name = name_row[0] if name_row else f"id={conflicting}"

    return jsonify({"error": f"{host_name}在這個時間有場次無法預定"}), 409


@app.post("/api/bookings")
@require_auth
def create_booking_route():
    """建立預約。

    request_id 是必填的冪等鍵，前端應該在玩家「按下預約」當下產生一組，
    該次意圖的所有重試都沿用同一組。刻意不允許省略：如果這裡自動產生，
    每次重試都會變成一次新的意圖，冪等保護就形同虛設——玩家網路不穩
    重送一次就可能佔到兩個名額，而名額是有限且被搶的。
    """
    body = request.get_json(silent=True) or {}

    request_id = (body.get("request_id") or "").strip()
    if not request_id:
        return jsonify({"error": "缺少 request_id（冪等鍵），請由前端產生一組 UUID"}), 400

    mmg_id = body.get("mmg_id")
    if not isinstance(mmg_id, int):
        return jsonify({"error": "需要 mmg_id"}), 400

    try:
        d, t = _parse_slot(body)
    except BookingError as exc:
        return jsonify({"error": str(exc)}), 400

    gm_user_ids = body.get("gm_user_ids")
    if not isinstance(gm_user_ids, list) or len(gm_user_ids) != 4:
        return jsonify({"error": "gm_user_ids 需要是長度 4 的陣列，沒有的角色填 null"}), 400

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
                    deposit=int(body.get("deposit") or 0),
                    note=body.get("note"),
                )
            except BookingError as exc:
                conn.rollback()
                return jsonify({"error": str(exc)}), 409
            booking = bookings_db.get_booking(cur, result.id)
            conn.commit()

    return jsonify({
        **booking,
        "position": result.position,
        "is_first": result.is_first,
        "label": result.label,
        # 重試時是 200 而不是 201——這一次沒有真的建立東西。
        "already_existed": result.already_existed,
    }), (200 if result.already_existed else 201)


@app.get("/api/bookings/mine")
@require_auth
def my_bookings():
    include_cancelled = request.args.get("include_cancelled") == "1"
    try:
        start = int(request.args.get("start", 1))
    except ValueError:
        return jsonify({"error": "start 必須是數字"}), 400
    if start < 1:
        return jsonify({"error": "start 必須大於 0"}), 400

    with pool.connection() as conn:
        with conn.cursor() as cur:
            return jsonify(bookings_db.list_player_bookings(
                cur, g.user["id"], include_cancelled=include_cancelled, start=start,
            ))


@app.get("/api/bookings/<int:booking_id>")
@require_auth
def get_booking_route(booking_id: int):
    with pool.connection() as conn:
        with conn.cursor() as cur:
            booking = bookings_db.get_booking(cur, booking_id)
    if booking is None:
        return jsonify({"error": "找不到這筆預約"}), 404
    if booking["player_id"] != g.user["id"] and g.user["role"] > 2:
        return jsonify({"error": "只能查看自己的預約"}), 403
    return jsonify(booking)


@app.post("/api/bookings/<int:booking_id>/cancel")
@require_auth
def cancel_booking_route(booking_id: int):
    """取消預約。玩家可以取消自己的，主持人與管理員可以取消任何一筆。

    取消之後後面的序位會自動往前遞補——序位是每次由 created_at 算出來的，
    這裡不需要、也沒有任何「把下一位升上來」的寫入。
    """
    body = request.get_json(silent=True) or {}
    with pool.connection() as conn:
        with conn.cursor() as cur:
            booking = bookings_db.get_booking(cur, booking_id)
            if booking is None:
                return jsonify({"error": "找不到這筆預約"}), 404
            if booking["player_id"] != g.user["id"] and g.user["role"] > 2:
                return jsonify({"error": "只能取消自己的預約"}), 403
            try:
                updated = bookings_db.cancel_booking(cur, booking_id, note=body.get("note"))
            except BookingError as exc:
                conn.rollback()
                return jsonify({"error": str(exc)}), 409
            conn.commit()
    return jsonify(updated)


@app.patch("/api/bookings/<int:booking_id>/status")
@require_role(2)
def set_booking_status(booking_id: int):
    """主持人與管理員更新預約狀態。取消請走 /cancel。"""
    body = request.get_json(silent=True) or {}
    status = (body.get("status") or "").strip()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            try:
                updated = bookings_db.set_status(
                    cur, booking_id, status, note=body.get("note")
                )
            except BookingError as exc:
                conn.rollback()
                return jsonify({"error": str(exc)}), 400
            conn.commit()
    return jsonify(updated)


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


@app.get("/api/host/dashboard")
@require_role(2)
def host_dashboard_route():
    """主持人介面初次載入：三個頁籤各第一頁，一次拿完。

    翻頁與篩選都不走這支——那兩件事只影響當前頁籤，走 /api/host/tab/<tab>
    單獨拿，另外兩個頁籤維持畫面上既有的內容、不重查。
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            return jsonify(bookings_db.host_dashboard(cur, g.user["id"]))


@app.get("/api/host/tab/<tab>")
@require_role(2)
def host_tab_route(tab: str):
    """單一頁籤的一頁資料。翻頁（start）與篩選都在這裡。

    start 是 1-based 起始筆數（1、51、101...）。換篩選條件時前端要把
    start 重設回 1——舊頁碼在新條件下可能根本不存在。
    """
    try:
        start = int(request.args.get("start", 1))
    except ValueError:
        return jsonify({"error": "start 必須是數字"}), 400
    if start < 1:
        return jsonify({"error": "start 必須大於 0"}), 400

    try:
        filters = _parse_filters(request.args)
        with pool.connection() as conn:
            with conn.cursor() as cur:
                return jsonify(
                    bookings_db.host_tab(cur, g.user["id"], tab, start=start, filters=filters)
                )
    except BookingError as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/bookings/<int:booking_id>/detail")
@require_role(2)
def booking_detail_route(booking_id: int):
    """主持人介面點「預定者名稱」或「指定的角色名稱」時要的資料。

    兩個對話框的內容合成一支：預定者的聯絡方式，以及這場所有角色與
    對應的主持人。分開做會變成點兩個地方各打一次 API，但兩者都是
    「這一筆預約的細節」，一次拿完比較合理。

    權限是 role<=2，但只有被指定為這場主持人的人（或管理員）能看——
    預定者的 email 與電話是個資，不該讓任何一位主持人查到別人的場次。
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.gm1_user_id, b.gm2_user_id, b.gm3_user_id, b.gm4_user_id,
                       b.gm1_confirmed, b.gm2_confirmed, b.gm3_confirmed, b.gm4_confirmed,
                       m.gm1_name, m.gm2_name, m.gm3_name, m.gm4_name,
                       u.name, u.email, u.line_id, u.phone,
                       m.name, b.session_date, b.session_time, b.status, b.note
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

            host_ids = list(row[0:4])
            if g.user["role"] > 1 and g.user["id"] not in host_ids:
                return jsonify({"error": "只能查看自己被指定的場次"}), 403

            # 把有指派的主持人名字一次查出來，避免四次單筆查詢
            present = [h for h in host_ids if h is not None]
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
                "user_id": host_ids[i],
                "user_name": names.get(host_ids[i]),
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

    回傳兩組（見 find_conflicts）：script 是這個劇本被排了兩場，hosts 是
    某位主持人同時段已經有別的場。兩者的嚴重程度不同——劇本撞期是排班
    問題，人可以判斷後放行；主持人自己撞期是他分身乏術，不該放行——所以
    分開給，不合併成一份清單。

    hosts 的每一筆帶 is_me，標出是不是呼叫者自己。主持人只在意自己撞到
    什麼。在這裡標而不是讓前端比對，是因為前端拿不到自己的 user id
    （登入回應只給 email/name/role），為了這件事改動登入回應的形狀不
    划算，而後端本來就知道自己是誰。
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            result = bookings_db.find_conflicts(cur, booking_id)
    for c in result["hosts"]:
        c["is_me"] = c["host_id"] == g.user["id"]
    return jsonify(result)


@app.post("/api/bookings/<int:booking_id>/confirm")
@require_role(2)
def confirm_booking_route(booking_id: int):
    """主持人確認自己在這場的指定。

    刻意不檢查「這個人是不是這場的主持人」——confirm_host() 的 WHERE
    已經包含那個條件，不符合就拋 BookingError。在這裡多做一次檢查，
    等於同一條規則有兩個地方可以改，遲早會不一致。
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            try:
                updated = bookings_db.confirm_host(cur, booking_id, g.user["id"])
            except BookingError as exc:
                conn.rollback()
                return jsonify({"error": str(exc)}), 409
            conn.commit()
    return jsonify(updated)


# ── 管理員介面 ──────────────────────────────────────────────────
#
# 全部 require_role(1)：只有管理員。跟主持人介面不同，這些路由看的是
# 全站資料，不是「呼叫者自己被指定的」。

def _admin_write(fn):
    """管理員寫入路由共用的殼：開連線、跑、成功就 commit、
    BookingError 回 400 並 rollback。"""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        body = request.get_json(silent=True) or {}
        with pool.connection() as conn:
            with conn.cursor() as cur:
                try:
                    result = fn(cur, body, *args, **kwargs)
                except BookingError as exc:
                    conn.rollback()
                    return jsonify({"error": str(exc)}), 400
                conn.commit()
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


@app.get("/api/admin/mmg")
@require_role(1)
def admin_list_mmg():
    """全部劇本，含停用的。同時附上可指派的主持人名單，讓前端的
    「新增主持人」下拉選單不用另外打一支 API。"""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            return jsonify({
                "items": mmg_admin.list_all(cur),
                "host_candidates": mmg_admin.list_host_candidates(cur),
                "rooms": mmg_admin.list_rooms(cur),
            })


@app.put("/api/admin/mmg/<int:mmg_id>")
@require_role(1)
@_admin_write
def admin_update_mmg(cur, body, mmg_id: int):
    changed = mmg_admin.update(cur, mmg_id, body)
    return jsonify({"changed": changed, "item": mmg_admin.get(cur, mmg_id)})


@app.post("/api/admin/mmg")
@require_role(1)
@_admin_write
def admin_create_mmg(cur, body):
    new_id = mmg_admin.create(cur, body)
    return jsonify(mmg_admin.get(cur, new_id)), 201


@app.get("/api/admin/users")
@require_role(1)
def admin_list_users():
    with pool.connection() as conn:
        with conn.cursor() as cur:
            return jsonify({"items": users_admin.list_all(cur)})


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
    """初次載入：五個子頁籤各第一頁。翻頁與篩選走 /tab/<tab>。"""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            return jsonify({
                "tabs": bookings_admin.TABS,
                "data": bookings_admin.overview(cur),
            })


@app.get("/api/admin/bookings/tab/<tab>")
@require_role(1)
def admin_bookings_tab(tab: str):
    try:
        start = int(request.args.get("start", 1))
    except ValueError:
        return jsonify({"error": "start 必須是數字"}), 400
    if start < 1:
        return jsonify({"error": "start 必須大於 0"}), 400

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


@app.post("/api/admin/bookings")
@require_role(1)
@_admin_write
def admin_create_booking(cur, body):
    """管理員手動建立場次。日期時間在這裡就解析成 date/time 物件，
    底下的 create/update 都以物件比對，不處理字串。"""
    d, t = _parse_slot(body)
    body = {**body, "session_date": d, "session_time": t}
    new_id = bookings_admin.create(cur, body)
    return jsonify(bookings_db.get_booking(cur, new_id)), 201


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
    changed = bookings_admin.update(cur, booking_id, data)
    return jsonify({"changed": changed, "item": bookings_db.get_booking(cur, booking_id)})


if __name__ == "__main__":
    # 預設 5001 不是 5000：macOS 的 AirPlay 接收器佔用 5000，
    # 會回 403 而不是連線被拒，症狀看起來像應用程式壞了。
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)))
