"""LINE 查詢回的圖片網址：7 天後過期、畫好的圖快取 10 分鐘（line_utils/images.py）。"""

from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def _fresh_cache(tokens):
    from line_utils import images

    images._cache.reset()
    yield
    images._cache.reset()


def _url(spec: str, *, issued: int, store: str = "shop-a") -> str:
    import tenancy
    from line_utils import images

    with tenancy.use(store):
        token = images.sign(spec, tenancy.current().line_channel_secret, issued=issued)
    return f"/v/{store}/line/img/{token}.png"


def _count_connections(monkeypatch) -> list:
    """把開資料庫連線的次數記下來（照樣真的連）。"""
    from db.connection import pool

    opened = []
    original = pool.connection

    def counting(*args, **kwargs):
        opened.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(pool, "connection", counting)
    return opened


def test_a_fresh_url_is_drawn_once_then_served_from_memory(client, monkeypatch):
    from line_utils import images

    opened = _count_connections(monkeypatch)
    url = _url("d20261121", issued=int(time.time()))
    first = client.get(url)
    assert first.status_code == 200 and first.mimetype == "image/png"
    max_age = int(first.headers["Cache-Control"].split("max-age=")[1])
    assert images.URL_TTL_SECONDS - 60 <= max_age <= images.URL_TTL_SECONDS, \
        "瀏覽器與代理最多留到網址過期為止"
    assert len(opened) == 1

    again = client.get(url)          # 例如預覽圖與原圖是同一個網址
    assert again.status_code == 200 and again.data == first.data
    assert len(opened) == 1, "快取命中不該再查資料庫、再畫一次"


def test_an_expired_url_is_gone_without_touching_the_database(client, monkeypatch, capsys):
    from line_utils import images

    opened = _count_connections(monkeypatch)
    r = client.get(_url("m202611-202612", issued=int(time.time()) - images.URL_TTL_SECONDS - 5),
                   headers={"User-Agent": "Line/26.0 iPhone"})
    assert r.status_code == 410 and "過期" in r.get_json()["error"]
    assert opened == []
    logged = capsys.readouterr().out
    assert "[line-img] m202611-202612" in logged and "已過期" in logged and "iPhone" in logged, \
        "過期的那幾次正是要量的資料（期限訂得對不對），要留在 log 裡"


def test_a_url_just_inside_the_window_still_works(client):
    from line_utils import images

    r = client.get(_url("d20261121", issued=int(time.time()) - images.URL_TTL_SECONDS + 60))
    assert r.status_code == 200
    assert int(r.headers["Cache-Control"].split("max-age=")[1]) <= 60


def test_a_tampered_url_is_rejected(client):
    url = _url("d20261121", issued=int(time.time()))
    assert client.get(url.replace("d20261121", "d20261122")).status_code == 404


def test_cached_images_are_drawn_again_after_the_cache_time(client, monkeypatch):
    from line_utils import images

    opened = _count_connections(monkeypatch)
    monkeypatch.setattr(images, "CACHE_SECONDS", 0.0)
    url = _url("d20261121", issued=int(time.time()))
    assert client.get(url).status_code == 200
    assert client.get(url).status_code == 200
    assert len(opened) == 2, "過了快取時間要照當下的資料重畫"


def test_the_cache_does_not_grow_without_bound(monkeypatch, app_module):
    import tenancy
    from line_utils import images

    monkeypatch.setattr(images, "_MAX_CACHE_BYTES", 1000)
    with tenancy.use("shop-a"):
        for i in range(10):
            images.remember(f"token-{i}", b"x" * 300)
        state = images._cache.get()
        assert state.size <= 1000 and len(state.images) <= 3
