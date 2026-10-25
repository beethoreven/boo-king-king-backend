"""劇本簡介頁：公開的內容、封面圖、限流、快取、瀏覽紀錄，以及兩家店分不分得開。"""

from __future__ import annotations

import io

import psycopg
import pytest
from PIL import Image

from tests.conftest import STORES

IP = {"X-Forwarded-For": "203.0.113.5"}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _png(size=(800, 600), color=(10, 120, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _sql(url: str, query: str, params=()) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(query, params)
        conn.commit()


@pytest.fixture(autouse=True)
def _fresh(tokens, pg_cluster):
    """每個測試前：兩家店的 1 號劇本回到上架、清掉簡介、清掉記憶體裡的快取與限流。"""
    import intro_public
    import tenancy
    from db import functions

    for db in ("shop_a", "shop_b"):
        _sql(pg_cluster[db], "UPDATE mmg SET status = 'active', intro_synopsis = NULL, "
             "intro_characters = NULL, intro_tags = '{}', cover_image = NULL, period = NULL, "
             "players = NULL, price = NULL, price_holiday = NULL, booking_cost = NULL, "
             "booking_cost_holiday = NULL, extra_note = NULL WHERE id = 1")
        _sql(pg_cluster[db], "UPDATE functions SET value = 'on' WHERE key = 'deposit'")
        _sql(pg_cluster[db], "DELETE FROM intro_view")
    for state in (intro_public._limiters, intro_public._caches, intro_public._views):
        state.reset()
    for store in ("shop-a", "shop-b"):
        with tenancy.use(store):
            functions.invalidate()


def _put_intro(client, token, store="shop-a", **body):
    return client.put(f"/v/{store}/api/admin/mmg/1/intro", headers=_auth(token), json=body)


# ── 內容 ───────────────────────────────────────────────────────


def test_public_intro_shows_what_the_store_wrote_and_what_the_script_has(client, tokens, pg_cluster):
    _sql(pg_cluster["shop_a"], "UPDATE mmg SET period = 4, players = '4男4女', price = 500, "
         "price_holiday = 650, booking_cost = 200, booking_cost_holiday = 200, "
         "extra_note = '需自備服裝' WHERE id = 1")
    r = _put_intro(client, tokens["a_admin"], synopsis=" 一場\r\n夢 ", characters="甲\n乙",
                   tags=["懸疑", " 懸疑 ", "", "情感"])
    assert r.status_code == 200, r.get_json()
    body = client.get("/v/shop-a/api/public/mmg/1", headers=IP).get_json()
    assert body["synopsis"] == "一場\n夢" and body["characters"] == "甲\n乙"
    assert body["tags"] == ["懸疑", "情感"], "空白與重複的標籤要略過"
    assert (body["period"], body["players"], body["price"], body["price_holiday"]) == (4.0, "4男4女", 500, 650)
    assert body["booking_cost"] == 200 and body["extra_note"] == "需自備服裝"
    assert body["cover_path"] is None


def test_missing_fields_are_absent_and_deposit_hidden_when_the_store_takes_none(client, pg_cluster):
    _sql(pg_cluster["shop_b"], "UPDATE mmg SET booking_cost = 300 WHERE id = 1")
    _sql(pg_cluster["shop_b"], "UPDATE functions SET value = 'off' WHERE key = 'deposit'")
    body = client.get("/v/shop-b/api/public/mmg/1", headers=IP).get_json()
    for key in ("period", "players", "price", "extra_note", "synopsis", "characters"):
        assert body[key] is None, key
    assert body["booking_cost"] is None, "不收訂金的店，簡介頁不該出現訂金"


@pytest.mark.parametrize("body, message", [
    ({"tags": [f"t{i}" for i in range(9)]}, "最多 8 個"),
    ({"tags": ["一二三四五六七八九十十一十二十三"]}, "每個最多 12 字"),
    ({"tags": "懸疑"}, "文字清單"),
    ({"synopsis": "字" * 3001}, "劇本簡介最多 3000 字"),
    ({"characters": 123}, "角色簡介必須是文字"),
])
def test_bad_intro_is_rejected_with_a_reason(client, tokens, body, message):
    r = _put_intro(client, tokens["a_admin"], **body)
    assert r.status_code == 400 and message in r.get_json()["error"]


def test_only_admins_can_edit(client, tokens):
    assert _put_intro(client, tokens["a_player"], synopsis="x").status_code == 403
    assert client.put("/v/shop-a/api/admin/mmg/1/intro", json={}).status_code == 401
    assert client.put("/v/shop-a/api/admin/mmg/1/cover", data=_png()).status_code == 401


def test_scripts_not_on_shelf_are_not_public_but_admins_still_see_them(client, tokens, pg_cluster):
    _sql(pg_cluster["shop_a"], "UPDATE mmg SET status = 'inactive' WHERE id = 1")
    assert client.get("/v/shop-a/api/public/mmg/1", headers=IP).status_code == 404
    r = client.get("/v/shop-a/api/admin/mmg/1/intro", headers=_auth(tokens["a_admin"]))
    assert r.status_code == 200 and r.get_json()["public"] is False


# ── 封面圖 ─────────────────────────────────────────────────────


def test_cover_upload_is_reencoded_and_served_with_a_versioned_url(client, tokens):
    h = _auth(tokens["a_admin"])
    first = client.put("/v/shop-a/api/admin/mmg/1/cover", headers=h, data=_png((3000, 2000)))
    assert first.status_code == 200, first.get_json()
    path1 = first.get_json()["cover_path"]
    img = client.get(f"/v/shop-a{path1}", headers=IP)
    assert img.status_code == 200 and img.mimetype == "image/webp"
    assert "immutable" in img.headers["Cache-Control"]
    assert Image.open(io.BytesIO(img.data)).size == (1200, 800), "等比例縮到最寬 1200"

    second = client.put("/v/shop-a/api/admin/mmg/1/cover", headers=h, data=_png(color=(1, 2, 3)))
    path2 = second.get_json()["cover_path"]
    assert path2 != path1, "換圖就要換網址，訪客才不會看到快取住的舊圖"
    assert client.get(f"/v/shop-a{path1}", headers=IP).status_code == 404

    gone = client.delete("/v/shop-a/api/admin/mmg/1/cover", headers=h).get_json()
    assert gone["cover_path"] is None
    assert client.get(f"/v/shop-a{path2}", headers=IP).status_code == 404


def test_a_real_photo_bigger_than_the_normal_request_limit_uploads(client, tokens):
    """全站請求上限是 64KB，只有上傳封面放寬到 5MB。真的照片幾乎都超過 64KB。"""
    import os as _os

    noise = Image.frombytes("RGB", (400, 400), _os.urandom(400 * 400 * 3))
    buf = io.BytesIO()
    noise.save(buf, "PNG")
    assert len(buf.getvalue()) > 64 * 1024
    r = client.put("/v/shop-a/api/admin/mmg/1/cover", headers=_auth(tokens["a_admin"]), data=buf.getvalue())
    assert r.status_code == 200, r.status_code


def test_cover_upload_rejects_non_images_and_huge_files(client, tokens):
    h = _auth(tokens["a_admin"])
    r = client.put("/v/shop-a/api/admin/mmg/1/cover", headers=h, data=b"<script>alert(1)</script>")
    assert r.status_code == 400 and "不是圖片" in r.get_json()["error"]
    r = client.put("/v/shop-a/api/admin/mmg/1/cover", headers=h, data=b"x" * (5 * 1024 * 1024 + 1))
    assert r.status_code == 413
    # 其他路徑仍然是 64KB 上限，沒有因為這一支放寬而跟著放寬。
    r = client.put("/v/shop-a/api/admin/mmg/1/intro", headers=h, data=b"x" * (65 * 1024),
                   content_type="application/json")
    assert r.status_code == 413


def test_admin_preview_cover_works_while_not_public(client, tokens, pg_cluster):
    h = _auth(tokens["a_admin"])
    path = client.put("/v/shop-a/api/admin/mmg/1/cover", headers=h, data=_png()).get_json()["cover_path"]
    _sql(pg_cluster["shop_a"], "UPDATE mmg SET status = 'preparing', start_booking = NOW() + interval '7 days' WHERE id = 1")
    import intro_public, tenancy
    with tenancy.use("shop-a"):
        intro_public.invalidate()
    assert client.get(f"/v/shop-a{path}", headers=IP).status_code == 404
    admin_path = path.replace("/api/public/", "/api/admin/")
    assert client.get(f"/v/shop-a{admin_path}", headers=h).status_code == 200


# ── 限流與快取 ─────────────────────────────────────────────────


def test_rate_limit_is_per_ip_and_per_store(client):
    import intro_public

    for _ in range(intro_public.INTRO_PER_MINUTE):
        assert client.get("/v/shop-a/api/public/mmg/1", headers=IP).status_code == 200
    r = client.get("/v/shop-a/api/public/mmg/1", headers=IP)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert client.get("/v/shop-a/api/public/mmg/1",
                      headers={"X-Forwarded-For": "198.51.100.7"}).status_code == 200, "別的 IP 不受影響"
    assert client.get("/v/shop-b/api/public/mmg/1", headers=IP).status_code == 200, "別家店不受影響"


def test_repeat_offenders_get_blocked_for_a_while(client):
    import intro_public

    for _ in range(intro_public.INTRO_PER_MINUTE):
        client.get("/v/shop-a/api/public/mmg/1", headers=IP)
    retries = [int(client.get("/v/shop-a/api/public/mmg/1", headers=IP).headers["Retry-After"])
               for _ in range(intro_public._STRIKES_TO_BLOCK)]
    assert retries[-1] >= 590, f"第 {intro_public._STRIKES_TO_BLOCK} 次被擋要封鎖 10 分鐘：{retries}"


def test_cached_views_and_unknown_ids_never_touch_the_database(client, monkeypatch):
    from db.connection import pool

    assert client.get("/v/shop-a/api/public/mmg/1", headers=IP).status_code == 200
    assert client.get("/v/shop-a/api/public/mmg/999", headers=IP).status_code == 404

    def boom(*_a, **_k):
        raise AssertionError("快取命中不該連資料庫")

    monkeypatch.setattr(pool, "connection", boom)
    assert client.get("/v/shop-a/api/public/mmg/1", headers=IP).status_code == 200
    assert client.get("/v/shop-a/api/public/mmg/999", headers=IP).status_code == 404


def test_same_script_number_in_two_stores_stays_separate(client, tokens):
    _put_intro(client, tokens["a_admin"], synopsis="A店的簡介")
    _put_intro(client, tokens["b_owner"], store="shop-b", synopsis="B店的簡介")
    a = client.get("/v/shop-a/api/public/mmg/1", headers=IP).get_json()
    b = client.get("/v/shop-b/api/public/mmg/1", headers=IP).get_json()
    assert (a["name"], a["synopsis"]) == ("A店的劇本", "A店的簡介")
    assert (b["name"], b["synopsis"]) == ("B店的劇本", "B店的簡介")
    # 版本號兩家店都從同一個數字開始——圖片快取的 key 撞在一起也要分得開。
    pa = client.put("/v/shop-a/api/admin/mmg/1/cover", headers=_auth(tokens["a_admin"]),
                    data=_png(color=(255, 0, 0))).get_json()["cover_path"]
    pb = client.put("/v/shop-b/api/admin/mmg/1/cover", headers=_auth(tokens["b_owner"]),
                    data=_png(color=(0, 0, 255))).get_json()["cover_path"]
    ra = Image.open(io.BytesIO(client.get(f"/v/shop-a{pa}", headers=IP).data)).convert("RGB").getpixel((5, 5))
    rb = Image.open(io.BytesIO(client.get(f"/v/shop-b{pb}", headers=IP).data)).convert("RGB").getpixel((5, 5))
    assert ra[0] > 200 and rb[2] > 200, (ra, rb)


# ── 瀏覽紀錄 ───────────────────────────────────────────────────


def _views(url: str) -> list[tuple]:
    with psycopg.connect(url) as conn:
        return conn.execute("SELECT mmg_id, ip, forwarded_for, user_agent FROM intro_view").fetchall()


def test_views_are_buffered_then_written_in_one_batch_per_store(client, pg_cluster, monkeypatch):
    import intro_public

    monkeypatch.setattr(intro_public, "FLUSH_AT", 3)
    # B 店先留一筆在暫存裡（還不到寫入的量）。暫存區要是全行程共用，下面 A 店
    # 觸發寫入時，這一筆會被一起寫進 A 店的資料庫。
    client.get("/v/shop-b/api/public/mmg/1", headers={"X-Forwarded-For": "198.51.100.99"})
    h = {"X-Forwarded-For": "203.0.113.9, 10.0.0.1", "User-Agent": "測試瀏覽器"}
    client.get("/v/shop-a/api/public/mmg/1", headers=h)
    client.get("/v/shop-a/api/public/mmg/1", headers=h)
    assert _views(pg_cluster["shop_a"]) == [], "還沒累積夠，不該寫資料庫"
    client.get("/v/shop-a/api/public/mmg/1", headers=h)
    rows = _views(pg_cluster["shop_a"])
    assert len(rows) == 3 and {r[1] for r in rows} == {"203.0.113.9"}, rows
    assert rows[0] == (1, "203.0.113.9", "203.0.113.9, 10.0.0.1", "測試瀏覽器")
    assert _views(pg_cluster["shop_b"]) == [], "B 店那一筆還在暫存，不該寫到任何地方"


def test_daily_job_writes_the_rest_and_purges_old_rows(client, pg_cluster):
    client.get("/v/shop-b/api/public/mmg/1", headers=IP)
    _sql(pg_cluster["shop_b"], "INSERT INTO intro_view (mmg_id, ip, viewed_at) "
         "VALUES (1, '192.0.2.1', NOW() - interval '91 days')")
    r = client.post("/v/shop-b/api/jobs/daily", headers={"X-Job-Token": STORES["shop-b"]["job_token"]})
    body = r.get_json()
    assert body["intro_views_saved"] == 1 and body["intro_views_purged"] == 1, body
    assert [row[1] for row in _views(pg_cluster["shop_b"])] == ["203.0.113.5"]


def test_uploaded_photo_loses_its_gps_location(client, tokens):
    """手機照片的 EXIF 常有 GPS 座標；公開的封面圖不能把店（或家）的位置帶出去。"""
    from PIL.TiffImagePlugin import IFDRational as R

    exif = Image.Exif()
    exif[0x0112] = 6                       # 要轉 90 度才是正的
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (R(25), R(2), R(0))
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (9, 9, 9)).save(buf, "JPEG", exif=exif.tobytes())
    path = client.put("/v/shop-a/api/admin/mmg/1/cover", headers=_auth(tokens["a_admin"]),
                      data=buf.getvalue()).get_json()["cover_path"]
    served = Image.open(io.BytesIO(client.get(f"/v/shop-a{path}", headers=IP).data))
    assert not dict(served.getexif()), "EXIF（含 GPS）要全部清掉"
    assert served.size == (300, 400), "照 EXIF 的方向轉正之後才丟掉 EXIF"



def test_client_ip_ignores_what_the_visitor_wrote_in_front(client, monkeypatch):
    """2026-10-26 在 demo 實測：Render 前面的代理把使用者自己送的 X-Forwarded-For
    留在最前面，再接上真正的 IP、Cloudflare、Render 內部。第一段不能信。"""
    import intro_public

    real = "36.227.139.114"
    def header(fake):
        return {"X-Forwarded-For": f"{fake},{real}, 172.71.81.179, 10.29.8.12"}

    # 每次換一個假的「第一段」，限流照樣算在同一個真 IP 上。
    for i in range(intro_public.INTRO_PER_MINUTE):
        assert client.get("/v/shop-a/api/public/mmg/1", headers=header(f"1.2.3.{i}")).status_code == 200
    assert client.get("/v/shop-a/api/public/mmg/1", headers=header("9.9.9.9")).status_code == 429

    # 塞一長串假值也擠不掉右邊可信的那一段。
    long_fake = ", ".join(f"5.5.5.{i % 250}" for i in range(400))
    from flask import Request
    from werkzeug.test import EnvironBuilder
    req = Request(EnvironBuilder(headers={"X-Forwarded-For": f"{long_fake},{real}, 172.71.81.179, 10.29.8.12"}).get_environ())
    ip, stored = intro_public.client_ip(req)
    assert ip == real and stored.endswith("10.29.8.12") and len(stored) <= 300
