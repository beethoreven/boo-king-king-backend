"""兩家店共用一個服務時，彼此的東西不能混在一起。

每一個測試都是「先讓 A 店把快取熱起來，馬上換 B 店」——快取全行程共用的
話，B 店讀到的就會是 A 店的。這正是 tests/test_tenant_state.py 防的那類錯誤
在結果上的樣子：那邊檢查寫法，這邊檢查結果，兩邊都要過。
"""

from __future__ import annotations

import base64
import json

import psycopg
import pytest

from tests.conftest import STORES

# session token 一律是 64 個小寫十六進位字元（見 auth_utils/sessions._TOKEN_SHAPE）。
TOKEN_A_ADMIN = "a1" * 32        # A 店：admin@shop.test，管理員
TOKEN_A_PLAYER = "a2" * 32       # A 店：player@shop.test，一般玩家
TOKEN_B_SAME_EMAIL = "b1" * 32   # B 店：admin@shop.test，在 B 店只是一般玩家
TOKEN_B_OWNER = "b2" * 32        # B 店：owner@shop.test，管理員


def _seed(url: str, *, users, sessions, mmg_name, site_name, gm_mode):
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        for email, name, role in users:
            cur.execute(
                "INSERT INTO users (email, name, role) VALUES (%s, %s, %s) "
                "ON CONFLICT (email) DO NOTHING",
                (email, name, role),
            )
        for token, email in sessions:
            cur.execute(
                "INSERT INTO sessions (token, email) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (token, email),
            )
        cur.execute("SELECT count(*) FROM mmg")
        if cur.fetchone()[0] == 0:
            cur.execute("INSERT INTO mmg (name) VALUES (%s)", (mmg_name,))
        cur.execute("UPDATE settings SET value = %s WHERE key = 'site_name'", (site_name,))
        cur.execute("UPDATE functions SET value = %s WHERE key = 'gm_mode'", (gm_mode,))
        conn.commit()


@pytest.fixture(scope="module", autouse=True)
def seeded(app_module, pg_cluster):
    _seed(
        pg_cluster["shop_a"],
        users=[("admin@shop.test", "A管理員", 1), ("player@shop.test", "A玩家", 3)],
        sessions=[(TOKEN_A_ADMIN, "admin@shop.test"), (TOKEN_A_PLAYER, "player@shop.test")],
        mmg_name="A店的劇本", site_name="A店", gm_mode="single",
    )
    _seed(
        pg_cluster["shop_b"],
        users=[("owner@shop.test", "B管理員", 1), ("admin@shop.test", "在B只是玩家", 3)],
        sessions=[(TOKEN_B_SAME_EMAIL, "admin@shop.test"), (TOKEN_B_OWNER, "owner@shop.test")],
        mmg_name="B店的劇本", site_name="B店", gm_mode="assign",
    )


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── 登入憑證與權限 ─────────────────────────────────────────────


def test_token_from_one_store_is_rejected_by_the_other(client):
    # B 店先有人用 admin@shop.test 登入過，B 店的權限快取裡就有這個 email。
    assert client.get("/v/shop-b/api/me", headers=_auth(TOKEN_B_SAME_EMAIL)).status_code == 200
    # A 店用同一個 email 的 token，讓 token → email 的快取熱起來。
    assert client.get("/v/shop-a/api/me", headers=_auth(TOKEN_A_ADMIN)).status_code == 200
    # 馬上拿去 B 店：B 店的資料庫裡沒有這張 token。token 快取全行程共用的話，
    # 這裡會直接命中、再接上 B 店權限快取裡的那個人——完全不查 B 店的資料庫。
    r = client.get("/v/shop-b/api/me", headers=_auth(TOKEN_A_ADMIN))
    assert r.status_code == 401, r.get_json()
    # 反方向也一樣。
    assert client.get("/v/shop-b/api/me", headers=_auth(TOKEN_B_OWNER)).status_code == 200
    assert client.get("/v/shop-a/api/me", headers=_auth(TOKEN_B_OWNER)).status_code == 401


def test_same_email_gets_each_stores_own_role(client):
    # admin@shop.test 在 A 店是管理員：權限快取會記下「這個 email 是 role 1」。
    r = client.get("/v/shop-a/api/admin/ping", headers=_auth(TOKEN_A_ADMIN))
    assert r.status_code == 200
    # 同一個 email 在 B 店只是玩家。快取全行程共用的話，這裡會拿到 A 店的 role 1。
    r = client.get("/v/shop-b/api/admin/ping", headers=_auth(TOKEN_B_SAME_EMAIL))
    assert r.status_code == 403, r.get_json()
    me = client.get("/v/shop-b/api/me", headers=_auth(TOKEN_B_SAME_EMAIL)).get_json()
    assert me["role"] == 3 and me["name"] == "在B只是玩家"


def test_banned_screen_shows_each_stores_own_admin(app_module):
    import tenancy
    from auth_utils.decorators import admin_display_name

    with tenancy.use("shop-a"):
        assert admin_display_name() == "A管理員"
    with tenancy.use("shop-b"):
        assert admin_display_name() == "B管理員"


def test_rate_limit_is_counted_per_store(app_module):
    import tenancy
    from auth_utils import guard

    with tenancy.use("shop-a"):
        guard.reset_rate()
        limited = None
        for _ in range(guard.RATE_LIMIT_PER_MINUTE + 1):
            limited = guard.check_rate(1)
        assert limited is not None, "A 店 1 號應該被限流了"
    try:
        with tenancy.use("shop-b"):
            # B 店的 1 號是另一個人，不該跟著被限流。
            assert guard.check_rate(1) is None
    finally:
        for store in ("shop-a", "shop-b"):
            with tenancy.use(store):
                guard.reset_rate()


def test_google_login_checks_the_stores_own_client_id(app_module, monkeypatch):
    import tenancy
    from auth_utils import google_auth

    seen = []

    def fake_verify(token, request, audience):
        seen.append(audience)
        return {"email_verified": True, "email": "someone@shop.test"}

    monkeypatch.setattr(google_auth.google_id_token, "verify_oauth2_token", fake_verify)
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "kid": "k"}).encode()).decode().rstrip("=")
    fake_jwt = f"{header}.e30.c2ln"
    for store in ("shop-a", "shop-b"):
        with tenancy.use(store):
            assert google_auth.verify_google_id_token(fake_jwt) == "someone@shop.test"
    assert seen == [STORES["shop-a"]["google_client_id"], STORES["shop-b"]["google_client_id"]]


# ── 快取與設定 ─────────────────────────────────────────────────


def test_slot_cache_does_not_mix_stores(client):
    """兩家店的劇本編號都從 1 開始。時段快取的 key 是 (劇本, 日期, 時間)，
    全行程共用的話，B 店查 1 號劇本會拿到 A 店 1 號劇本的名稱。"""
    params = "session_date=2031-01-07&session_time=19:00"
    a = client.get(f"/v/shop-a/api/mmg/1/slot?{params}", headers=_auth(TOKEN_A_PLAYER))
    b = client.get(f"/v/shop-b/api/mmg/1/slot?{params}", headers=_auth(TOKEN_B_SAME_EMAIL))
    assert a.status_code == 200 and b.status_code == 200, (a.get_json(), b.get_json())
    assert a.get_json()["mmg_name"] == "A店的劇本"
    assert b.get_json()["mmg_name"] == "B店的劇本"


def test_settings_and_functions_are_read_per_store(app_module):
    import notifications
    import tenancy
    from db import functions
    from db.connection import pool

    results = {}
    for store in ("shop-a", "shop-b", "shop-a"):   # 回頭再讀一次 A：快取命中也要是 A 的
        with tenancy.use(store), pool.connection() as conn, conn.cursor() as cur:
            results.setdefault(store, []).append(
                (functions.gm_mode(cur), notifications.settings(cur)["site_name"])
            )
    assert results["shop-a"] == [("single", "A店"), ("single", "A店")]
    assert results["shop-b"] == [("assign", "B店")]


def test_mail_carries_its_store_and_sender(app_module):
    import notifications
    import tenancy
    from db.connection import pool

    for store, site in (("shop-a", "A店"), ("shop-b", "B店")):
        with tenancy.use(store), pool.connection() as conn, conn.cursor() as cur:
            key = next(k for k, t in notifications.templates(cur).items() if t["enabled"])
            msg = notifications.build(cur, key, [{"email": "to@shop.test"}], {})
            assert msg.tenant == store
            # 寄件人名稱沒設時退回店名（見 notifications.build）。
            assert msg.sender["name"] in (site, notifications.settings(cur).get("sender_name"))


def test_schema_was_built_in_every_store(pg_cluster, app_module):
    for db in ("shop_a", "shop_b"):
        with psycopg.connect(pg_cluster[db]) as conn:
            cols = {r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'mmg'"
            )}
        assert "extra_note" in cols, f"{db} 沒有被補上新欄位"


# ── 網址、來源、排程、LINE ─────────────────────────────────────


def test_unknown_store_is_404_without_listing_stores(client):
    r = client.get("/v/zzz/api/ready")
    assert r.status_code == 404
    text = r.get_data(as_text=True)
    assert "shop-a" not in text and "shop-b" not in text


def test_missing_prefix_is_404_except_health(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/mmg").status_code == 404


def test_frontend_of_one_store_cannot_call_the_other(client):
    other = STORES["shop-a"]["origin"]
    r = client.get("/v/shop-b/api/me", headers={**_auth(TOKEN_B_OWNER), "Origin": other})
    assert r.status_code == 403
    own = STORES["shop-b"]["origin"]
    r = client.get("/v/shop-b/api/me", headers={**_auth(TOKEN_B_OWNER), "Origin": own})
    assert r.status_code == 200
    assert r.headers.get("Access-Control-Allow-Origin") == own
    # 預檢（OPTIONS）也一樣要擋。
    r = client.options("/v/shop-b/api/me", headers={
        "Origin": other, "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization",
    })
    assert r.status_code == 403


def test_job_token_only_works_for_its_own_store(client):
    a_token = STORES["shop-a"]["job_token"]
    b_token = STORES["shop-b"]["job_token"]
    assert client.post("/v/shop-b/api/jobs/weekly", headers={"X-Job-Token": a_token}).status_code == 401
    assert client.post("/v/shop-b/api/jobs/weekly", headers={"X-Job-Token": b_token}).status_code == 200
    assert client.get("/v/shop-a/internal/idle-gap-stats", headers={"X-Job-Token": b_token}).status_code == 401


def test_line_webhook_checks_the_stores_own_secret(client):
    from line_utils import signature

    body = b'{"events":[]}'
    signed_by_a = signature.sign(body, STORES["shop-a"]["line_secret"])
    signed_by_b = signature.sign(body, STORES["shop-b"]["line_secret"])
    headers = {"Content-Type": "application/json"}
    r = client.post("/v/shop-b/line/webhook", data=body, headers={**headers, "X-Line-Signature": signed_by_a})
    assert r.status_code == 401
    r = client.post("/v/shop-b/line/webhook", data=body, headers={**headers, "X-Line-Signature": signed_by_b})
    assert r.status_code == 200


# ── 「目前是哪家店」本身 ───────────────────────────────────────


def test_store_is_cleared_after_each_request(client):
    import tenancy

    client.get("/v/shop-a/api/ready")
    # Flask 的測試客戶端跟測試跑在同一條執行緒——請求結束後這條執行緒上
    # 不該還留著 A 店。gunicorn 的執行緒會被重複使用，留著就會漏給下一個請求。
    assert tenancy.current_or_none() is None


def test_no_store_means_no_database_and_no_cache(app_module):
    import tenancy
    from db.connection import pool

    with pytest.raises(tenancy.NoTenant):
        with pool.connection():
            pass
    with pytest.raises(tenancy.NoTenant):
        tenancy.PerTenant(dict).get()
