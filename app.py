"""
booking-king 後端。

目前只有基礎建設：健康檢查、保活端點、以及 Google 登入與 session 層。
業務路由（劇本、場次、預約）尚未加入。
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

# 一定要在 import 任何會讀 os.environ 的模組之前載入。db.connection 在
# import 當下就會讀 DATABASE_URL，晚一步就是 KeyError。
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from flask import Flask, g, jsonify, request  # noqa: E402
from flask_cors import CORS  # noqa: E402

from auth_utils import sessions, whitelist  # noqa: E402
from auth_utils.decorators import require_auth, require_role  # noqa: E402
from auth_utils.google_auth import extract_bearer_token, verify_google_id_token  # noqa: E402
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
            user = whitelist.get_user(cur, email)
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


if __name__ == "__main__":
    # 預設 5001 不是 5000：macOS 的 AirPlay 接收器佔用 5000，
    # 會回 403 而不是連線被拒，症狀看起來像應用程式壞了。
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)))
