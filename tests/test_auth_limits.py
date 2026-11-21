"""登入之前就要擋的限流（auth_utils/ip_guard.py）與每個 email 的 session 上限
（auth_utils/sessions.py）。用兩家測試店的臨時資料庫實際打一次。"""

from __future__ import annotations

import datetime as dt

import psycopg

IP ={"X-Forwarded-For": "203.0.113.5"}
OTHER_IP = {"X-Forwarded-For": "198.51.100.7"}


def _auth(token: str, extra: dict | None = None) -> dict:
    return {"Authorization": f"Bearer {token}", **(extra or {})}


# ── 登入、註冊、狀態查詢 ─────────────────────────────────────────


def test_status_is_limited_per_ip_and_per_store(client, tokens):
    from auth_utils import ip_guard

    h = _auth(tokens["a_player"], IP)
    for _ in range(ip_guard.STATUS_PER_MINUTE):
        assert client.get("/v/shop-a/auth/status", headers=h).status_code == 200
    r = client.get("/v/shop-a/auth/status", headers=h)
    assert r.status_code == 429 and r.get_json()["code"] == "rate_limited"
    assert int(r.headers["Retry-After"]) >= 1
    assert client.get("/v/shop-a/auth/status",
                      headers=_auth(tokens["a_player"], OTHER_IP)).status_code == 200, "別的 IP 不受影響"
    assert client.get("/v/shop-b/auth/status",
                      headers=_auth(tokens["b_owner"], IP)).status_code == 200, "別家店不受影響"


def test_login_is_limited_before_google_is_asked(client, app_module, monkeypatch):
    """被擋下來的登入連 Google 憑證都不驗——驗憑證與寫 session 正是要省的東西。"""
    from auth_utils import ip_guard

    asked = []
    monkeypatch.setattr(app_module, "verify_google_id_token", lambda t: asked.append(t))
    for _ in range(ip_guard.LOGIN_PER_MINUTE):
        assert client.post("/v/shop-a/auth/login", json={"id_token": "x"}, headers=IP).status_code == 401
    assert len(asked) == ip_guard.LOGIN_PER_MINUTE
    assert client.post("/v/shop-a/auth/login", json={"id_token": "x"}, headers=IP).status_code == 429
    assert len(asked) == ip_guard.LOGIN_PER_MINUTE, "被擋的那一次不該去驗 Google 憑證"


def test_register_is_limited(client, tokens):
    from auth_utils import ip_guard

    h = _auth(tokens["a_player"], IP)   # 已經註冊過的人：每次都回 400，一樣算次數
    for _ in range(ip_guard.REGISTER_PER_MINUTE):
        assert client.post("/v/shop-a/auth/register", json={}, headers=h).status_code == 400
    assert client.post("/v/shop-a/auth/register", json={}, headers=h).status_code == 429


def test_repeat_offenders_on_login_are_blocked_for_a_while(client):
    import ip_limit
    from auth_utils import ip_guard

    for _ in range(ip_guard.LOGIN_PER_MINUTE):
        client.post("/v/shop-a/auth/login", json={"id_token": "x"}, headers=IP)
    retries = [int(client.post("/v/shop-a/auth/login", json={"id_token": "x"},
                               headers=IP).headers["Retry-After"])
               for _ in range(ip_limit.STRIKES_TO_BLOCK)]
    assert retries[-1] >= 590, retries
    # 封鎖的是登入這一組（登入、註冊、狀態查詢），不是整個網站的 API。
    assert client.get("/v/shop-a/api/me", headers={**IP, "Authorization": "Bearer x"}).status_code == 401


def test_ipv6_is_counted_per_64(client, tokens):
    from auth_utils import ip_guard

    def status(addr):
        return client.get("/v/shop-a/auth/status",
                          headers=_auth(tokens["a_player"], {"X-Forwarded-For": addr})).status_code

    for i in range(ip_guard.STATUS_PER_MINUTE):
        assert status(f"2001:db8:1:2::{i + 1:x}") == 200
    assert status("2001:db8:1:2:ffff:ffff:ffff:ffff") == 429, "同一個 /64 換位址不能重新計算"
    assert status("2001:db8:1:3::1") == 200, "隔壁的 /64 是別人"


# ── 已登入的 API：同一個 IP 的總量 ───────────────────────────────


def test_several_accounts_on_one_ip_share_one_budget(client, tokens, monkeypatch):
    """一個人開好幾個帳號，每個帳號各有自己的額度；IP 總量上限把它們加在一起算。"""
    from auth_utils import ip_guard

    monkeypatch.setattr(ip_guard, "API_PER_MINUTE", 6)
    for _ in range(3):
        assert client.get("/v/shop-a/api/me", headers=_auth(tokens["a_player"], IP)).status_code == 200
        assert client.get("/v/shop-a/api/me", headers=_auth(tokens["a_admin"], IP)).status_code == 200
    r = client.get("/v/shop-a/api/me", headers=_auth(tokens["a_player"], IP))
    assert r.status_code == 429 and r.get_json()["code"] == "rate_limited"
    assert client.get("/v/shop-a/api/me",
                      headers=_auth(tokens["a_player"], OTHER_IP)).status_code == 200


def test_api_budget_throttles_but_never_blocks(client, tokens, monkeypatch):
    """同一個 IP 後面可能真的有好幾個人，所以只限流、不封鎖十分鐘。"""
    from auth_utils import ip_guard

    monkeypatch.setattr(ip_guard, "API_PER_MINUTE", 2)
    h = _auth(tokens["a_player"], IP)
    for _ in range(2):
        client.get("/v/shop-a/api/me", headers=h)
    retries = [int(client.get("/v/shop-a/api/me", headers=h).headers["Retry-After"]) for _ in range(5)]
    assert max(retries) <= 61, retries


def test_forged_tokens_do_not_use_up_the_shared_budget(client, tokens, monkeypatch):
    from auth_utils import ip_guard

    monkeypatch.setattr(ip_guard, "API_PER_MINUTE", 3)
    for _ in range(20):
        assert client.get("/v/shop-a/api/me", headers=_auth("0" * 64, IP)).status_code == 401
    assert client.get("/v/shop-a/api/me", headers=_auth(tokens["a_player"], IP)).status_code == 200


# ── 每個 email 的 session 上限 ───────────────────────────────────


def _session_tokens(url: str, email: str) -> list[str]:
    with psycopg.connect(url) as conn:
        rows = conn.execute("SELECT token FROM sessions WHERE email = %s", (email,)).fetchall()
    return [r[0] for r in rows]


def test_logging_in_again_and_again_keeps_at_most_ten_sessions(client, app_module, pg_cluster, monkeypatch):
    from auth_utils import sessions

    email = "again-and-again@shop.test"
    monkeypatch.setattr(app_module, "verify_google_id_token", lambda t: email)
    issued = [client.post("/v/shop-a/auth/login", json={"id_token": "x"}, headers=IP).get_json()["session_token"]
              for _ in range(sessions.MAX_SESSIONS_PER_EMAIL + 3)]
    kept = _session_tokens(pg_cluster["shop_a"], email)
    assert len(kept) == sessions.MAX_SESSIONS_PER_EMAIL
    assert issued[-1] in kept, "剛登入的那一張一定要留著"
    assert client.get("/v/shop-a/auth/status", headers=_auth(issued[0], IP)).status_code == 401, \
        "被擠掉的那張不能再用"


def test_the_least_recently_used_session_goes_first_not_the_oldest(app_module, pg_cluster):
    """還在用的裝置不能因為「很早就登入了」而被擠掉。"""
    import tenancy
    from auth_utils import sessions

    email = "many-devices@shop.test"
    url = pg_cluster["shop_a"]
    with tenancy.use("shop-a"), psycopg.connect(url) as conn, conn.cursor() as cur:
        first = [sessions.create_session(cur, email) for _ in range(sessions.MAX_SESSIONS_PER_EMAIL)]
        now = dt.datetime.now(dt.timezone.utc)
        # 第一張最早發、但剛剛還在用；其他張依序越來越久沒用。發的時間也各不相同——
        # 同一個交易裡發的幾張時間一模一樣，不設的話分不出「照發的時間」與「照用的時間」。
        for i, token in enumerate(first):
            created = now - dt.timedelta(minutes=200 - i)
            last_seen = now - dt.timedelta(minutes=1 if i == 0 else 100 - i)
            cur.execute("UPDATE sessions SET created_at = %s, last_seen_at = %s WHERE token = %s",
                        (created, last_seen, token))
        newest = sessions.create_session(cur, email)
        conn.commit()
    kept = set(_session_tokens(url, email))
    assert first[0] in kept, "最早發、但最近還在用的那張要留著"
    assert first[1] not in kept, "最久沒用的那張被擠掉"
    assert newest in kept and len(kept) == sessions.MAX_SESSIONS_PER_EMAIL


def test_other_emails_are_not_touched(app_module, pg_cluster, tokens):
    import tenancy
    from auth_utils import sessions

    url = pg_cluster["shop_a"]
    before = _session_tokens(url, "player@shop.test")
    with tenancy.use("shop-a"), psycopg.connect(url) as conn, conn.cursor() as cur:
        for _ in range(sessions.MAX_SESSIONS_PER_EMAIL + 2):
            sessions.create_session(cur, "someone-else@shop.test")
        conn.commit()
    assert _session_tokens(url, "player@shop.test") == before
