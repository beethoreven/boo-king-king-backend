"""
booking-king 後端。

健康檢查、保活端點、Google 登入與 session 層，以及劇本查詢與預約路由。
"""

from __future__ import annotations

import os
from datetime import date, time

from dotenv import load_dotenv

# 一定要在 import 任何會讀 os.environ 的模組之前載入。db.connection 在
# import 當下就會讀 DATABASE_URL，晚一步就是 KeyError。
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from flask import Flask, g, jsonify, request  # noqa: E402
from flask_cors import CORS  # noqa: E402

from auth_utils import sessions, whitelist  # noqa: E402
from auth_utils.decorators import require_auth, require_role  # noqa: E402
from auth_utils.google_auth import extract_bearer_token, verify_google_id_token  # noqa: E402
from db import bookings as bookings_db  # noqa: E402
from db import mmg as mmg_db  # noqa: E402
from db.bookings import BookingError  # noqa: E402
from db.connection import pool  # noqa: E402
from db.schema import ensure_schema  # noqa: E402

app = Flask(__name__)
CORS(app)

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

    capacity = 1 + item["waitlist_limit"]
    return jsonify({
        "mmg_id": mmg_id,
        "mmg_name": item["name"],
        "session_date": d.isoformat(),
        "session_time": t.strftime("%H:%M"),
        "capacity": capacity,
        "taken": len(queue),
        "is_full": len(queue) >= capacity,
        "queue": [
            {"position": p, "booking_id": bid, "player_name": nm,
             "status": st, "is_confirmed": p == 1}
            for p, bid, nm, st, _dep, _ts, _rq in queue
        ],
    })


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

    # 玩家一律是自己。主持人與管理員（role <= 2）可以代訂——電話預約
    # 是真實情境。但一般玩家不能指定別人，否則就能用別人的名義佔位。
    player_id = g.user["id"]
    if body.get("player_id") is not None:
        if g.user["role"] > 2:
            return jsonify({"error": "只有主持人或管理員可以代其他玩家預約"}), 403
        player_id = body["player_id"]

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
        "is_confirmed": result.is_confirmed,
        "label": result.label,
        # 重試時是 200 而不是 201——這一次沒有真的建立東西。
        "already_existed": result.already_existed,
    }), (200 if result.already_existed else 201)


@app.get("/api/bookings/mine")
@require_auth
def my_bookings():
    include_cancelled = request.args.get("include_cancelled") == "1"
    with pool.connection() as conn:
        with conn.cursor() as cur:
            rows = bookings_db.list_player_bookings(
                cur, g.user["id"], include_cancelled=include_cancelled
            )
    return jsonify(rows)


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

    取消之後候補會自動遞補——順位是每次由 created_at 算出來的，
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


if __name__ == "__main__":
    # 預設 5001 不是 5000：macOS 的 AirPlay 接收器佔用 5000，
    # 會回 403 而不是連線被拒，症狀看起來像應用程式壞了。
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)))
