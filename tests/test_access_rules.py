"""幾條「誰讀得到什麼」的規則，用兩家測試店的臨時資料庫實際打一次：

  - 沒上架、已下架的劇本，預約畫面的三支都當作不存在（db/mmg.get）
  - /api/bookings/check-gm 只能問這齣戲可選的主持人（管理員不限、問自己可以）
  - /api/ready 連不上資料庫時，不把驅動程式的錯誤原文回給呼叫端
  - 管理員把人降級時，那個人要重新登入
"""

from __future__ import annotations

import logging

import psycopg
import pytest

TAIPEI_NOW = "(NOW() AT TIME ZONE 'Asia/Taipei')"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _sql(url: str, query: str, params=()):
    with psycopg.connect(url) as conn:
        cur = conn.execute(query, params)
        row = cur.fetchone() if cur.description else None
        conn.commit()
    return row


@pytest.fixture()
def script_one(pg_cluster):
    """測試裡會改兩家店 1 號劇本的上下架與主持人名單；結束時改回原樣。"""
    cols = "status, start_booking, end_booking, gm1_name, gm1_user"
    saved = {db: _sql(pg_cluster[db], f"SELECT {cols} FROM mmg WHERE id = 1") for db in ("shop_a", "shop_b")}
    yield
    for db, (status, start, end, gm1_name, gm1_user) in saved.items():
        _sql(pg_cluster[db], "UPDATE mmg SET status = %s, start_booking = %s, end_booking = %s, "
             "gm1_name = %s, gm1_user = %s WHERE id = 1", (status, start, end, gm1_name, gm1_user))


def _clear_slot_cache(store: str) -> None:
    import tenancy
    from db import slot_cache

    with tenancy.use(store):
        slot_cache.clear()


# ── 沒上架、已下架的劇本 ─────────────────────────────────────────


PLAYER_PATHS = (
    "/v/shop-a/api/mmg/1",
    "/v/shop-a/api/mmg/1/availability?from=2026-12-01&to=2026-12-31",
    "/v/shop-a/api/mmg/1/slot?session_date=2026-12-05&session_time=14:00",
)


@pytest.mark.parametrize("state, setup", [
    ("下架", "UPDATE mmg SET status = 'inactive' WHERE id = 1"),
    ("還沒開賣", f"UPDATE mmg SET status = 'preparing', start_booking = {TAIPEI_NOW} + interval '30 days' WHERE id = 1"),
    ("預約已截止", f"UPDATE mmg SET end_booking = {TAIPEI_NOW} - interval '1 hour' WHERE id = 1"),
])
def test_scripts_players_cannot_see_are_not_readable_by_number(client, tokens, pg_cluster, script_one, state, setup):
    url = pg_cluster["shop_a"]
    _sql(url, f"UPDATE mmg SET status = 'active', start_booking = {TAIPEI_NOW} - interval '2 days', "
              "end_booking = NULL WHERE id = 1")
    _clear_slot_cache("shop-a")
    assert client.get(PLAYER_PATHS[0], headers=_auth(tokens["a_player"])).status_code == 200

    _sql(url, setup)
    _clear_slot_cache("shop-a")
    for path in PLAYER_PATHS:
        r = client.get(path, headers=_auth(tokens["a_player"]))
        assert r.status_code == 404, f"{state}：{path} 回了 {r.status_code}"
    # 管理員看全部劇本走 /api/admin/mmg；預約畫面這三支對誰都一樣。
    assert client.get(PLAYER_PATHS[0], headers=_auth(tokens["a_admin"])).status_code == 404
    assert client.get("/v/shop-a/api/admin/mmg", headers=_auth(tokens["a_admin"])).status_code == 200


# ── check-gm ────────────────────────────────────────────────────


def test_check_gm_answers_only_for_hosts_of_that_script(client, tokens, pg_cluster, script_one):
    url = pg_cluster["shop_b"]
    host_id = _sql(url, "INSERT INTO users (email, name, role) VALUES ('host@shop.test', 'B主持', 2) "
                        "ON CONFLICT (email) DO UPDATE SET role = 2 RETURNING id")[0]
    owner_id = _sql(url, "SELECT id FROM users WHERE email = 'owner@shop.test'")[0]
    player_id = _sql(url, "SELECT id FROM users WHERE email = 'admin@shop.test'")[0]   # 在 B 店是玩家
    _sql(url, f"UPDATE mmg SET status = 'active', start_booking = {TAIPEI_NOW} - interval '2 days', "
              "end_booking = NULL, gm1_name = '主持', gm1_user = ARRAY[%s]::int[] WHERE id = 1", (host_id,))

    def ask(user_id, token):
        return client.get("/v/shop-b/api/bookings/check-gm", headers=_auth(token), query_string={
            "mmg_id": 1, "user_id": user_id, "session_date": "2026-12-05", "session_time": "14:00"})

    player, admin = tokens["b_same_email"], tokens["b_owner"]
    assert ask(host_id, player).get_json() == {"ok": True}, "這齣戲可選的主持人：照常回答"
    r = ask(owner_id, player)
    assert r.status_code == 403 and "可選的主持人" in r.get_json()["error"]
    assert ask(player_id, player).status_code == 200, "問自己可以"
    assert ask(owner_id, admin).status_code == 200, "管理員不受限"

    _sql(url, "UPDATE mmg SET status = 'inactive' WHERE id = 1")
    assert ask(host_id, player).status_code == 404, "下架的劇本，玩家連它的主持人也問不到"
    assert ask(host_id, admin).status_code == 200


# ── /api/ready ──────────────────────────────────────────────────


def test_ready_does_not_echo_driver_errors(client, monkeypatch, caplog):
    from db.connection import pool

    secret = "pw-that-must-not-leak"

    def broken(*args, **kwargs):
        raise psycopg.OperationalError(
            f'invalid connection option "postgresql://owner:{secret}@db.example.test/neondb"')

    monkeypatch.setattr(pool, "connection", broken)
    monkeypatch.setattr(psycopg, "connect", broken)
    caplog.set_level(logging.WARNING)
    r = client.get("/v/shop-a/api/ready", headers={"X-Job-Token": "job-token-a"})
    assert r.status_code == 503
    body = r.get_data(as_text=True)
    assert secret not in body and "db.example.test" not in body and "invalid connection option" not in body
    data = r.get_json()
    assert "OperationalError" in data["database"] and "OperationalError" in data["database_detail"]
    assert secret not in caplog.text, "寫進 log 的原文也要遮掉密碼"
    assert "db.example.test" in caplog.text, "其他排查用的內容照樣留在 log"


@pytest.mark.parametrize("raw, leaked", [
    ("postgresql://owner:hunter2@ep-x.neon.tech/db", "hunter2"),
    ('could not connect: "postgres://u:p%40ss@h:5432/db?sslmode=require"', "p%40ss"),
    ("host=h user=u password=hunter2 dbname=d", "hunter2"),
    ("password='two words' host=h", "two words"),
    ("postgresql://owner:pa/ss@ep-x.neon.tech/db", "pa/ss"),      # 密碼裡有 /
])
def test_secrets_are_redacted_from_logged_errors(app_module, raw, leaked):
    assert leaked not in app_module._redact_secrets(raw)


# ── 降級 ────────────────────────────────────────────────────────


def test_downgrading_someone_signs_them_out_but_other_edits_do_not(client, tokens, pg_cluster):
    import tenancy
    from auth_utils import sessions

    url = pg_cluster["shop_a"]
    with tenancy.use("shop-a"), psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO users (email, name, role) VALUES ('host-a@shop.test', 'A主持', 2) "
                    "ON CONFLICT (email) DO UPDATE SET role = 2, status = 'active' RETURNING id")
        host_id = cur.fetchone()[0]
        host_token = sessions.create_session(cur, "host-a@shop.test")
        conn.commit()

    def edit(role, name="A主持"):
        return client.put(f"/v/shop-a/api/admin/users/{host_id}", headers=_auth(tokens["a_admin"]), json={
            "email": "host-a@shop.test", "name": name, "role": role, "status": "active",
            "line_id": None, "phone": None})

    def me():
        return client.get("/v/shop-a/api/me", headers=_auth(host_token))

    assert me().status_code == 200
    assert edit(2, name="改了名字的主持").status_code == 200
    assert me().status_code == 200, "改名字不該讓人重新登入"
    assert edit(1).status_code == 200
    assert me().get_json()["role"] == 1, "升級不該讓人重新登入"
    assert edit(3).status_code == 200
    assert me().status_code == 401, "降級之後那張 session 就不能用了"
