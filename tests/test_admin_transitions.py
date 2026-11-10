"""管理員改狀態的那幾條規則（2026-11-10），用臨時資料庫實際跑一次。

    1. 一建立就成立的店（不收訂金＋唯一主持人），一個時段只收一組
    2. 改回「待主持確認」會清掉主持人的確認，並寄「請重新確認」
    3. 把已取消的改回來之前，序位預覽要講對
    4. 下架、停用之前列的「未來場次」要講對

每個測試都在同一個交易裡做完就 rollback，不留資料給別的測試。
"""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg
import pytest


@pytest.fixture()
def shop(app_module, tokens, pg_cluster):
    """回傳 (cur, 工具)。store 參數用 request.param 不好讀，這裡直接給兩家。"""
    import tenancy

    opened = []

    def use(store: str):
        db = {"shop-a": "shop_a", "shop-b": "shop_b"}[store]
        ctx = tenancy.use(store)
        ctx.__enter__()
        conn = psycopg.connect(pg_cluster[db])
        opened.append((ctx, conn))
        return conn.cursor()

    yield use
    from db import functions, roles
    for ctx, conn in opened:
        conn.rollback()
        conn.close()
        functions.invalidate()
        roles.invalidate()
        ctx.__exit__(None, None, None)


def _set_function(cur, key, value):
    from db import functions
    cur.execute("UPDATE functions SET value = %s WHERE key = %s", (value, key))
    functions.invalidate()


def _mmg(cur, **cols) -> int:
    cur.execute("SELECT id FROM mmg ORDER BY id LIMIT 1")
    mmg = cur.fetchone()[0]
    sets = ", ".join(f"{k} = %s" for k in cols)
    cur.execute(f"UPDATE mmg SET {sets} WHERE id = %s", (*cols.values(), mmg))
    return mmg


def _user(cur, email, role=3) -> int:
    cur.execute(
        "INSERT INTO users (email, name, role) VALUES (%s, %s, %s) RETURNING id",
        (email, email.split("@")[0], role),
    )
    return cur.fetchone()[0]


def _booking(cur, mmg, player, d, t, status, *, gm=None, confirmed=False, created=None) -> int:
    cur.execute(
        "INSERT INTO booking_record (mmg_id, player_id, session_date, session_time, day_type,"
        " status, gm1_user_id, gm1_confirmed, request_id, created_at)"
        " VALUES (%s,%s,%s,%s,'weekday',%s,%s,%s,%s, COALESCE(%s, NOW())) RETURNING id",
        (mmg, player, d, t, status, gm, confirmed, str(uuid.uuid4()), created),
    )
    return cur.fetchone()[0]


def _admin_data(cur, booking_id, **over) -> dict:
    cur.execute(
        "SELECT mmg_id, player_id, session_date, session_time, status, deposit, note,"
        " gm1_user_id, gm2_user_id, gm3_user_id, gm4_user_id,"
        " gm1_confirmed, gm2_confirmed, gm3_confirmed, gm4_confirmed, day_type"
        " FROM booking_record WHERE id = %s",
        (booking_id,),
    )
    r = cur.fetchone()
    data = {
        "mmg_id": r[0], "player_id": r[1], "session_date": r[2], "session_time": r[3],
        "status": r[4], "deposit": r[5], "note": r[6],
        "gm_user_ids": list(r[7:11]), "gm_confirmed": list(r[11:15]), "day_type": r[15],
    }
    data.update(over)
    return data


FUTURE = dt.date.today() + dt.timedelta(days=60)
PAST = dt.date.today() - dt.timedelta(days=3)


# ── 1. 一建立就成立的店，一個時段只收一組 ──────────────────────────

def test_instant_store_takes_one_group_per_slot(shop):
    from db import bookings
    from db.bookings import BookingError

    cur = shop("shop-a")                      # shop-a 是 single 模式
    _set_function(cur, "deposit", "off")
    mmg = _mmg(cur, waitlist_limit=3, gm1_name="主持")   # 規則出現前就存好的舊數字
    p1, p2 = _user(cur, "p1@a.test"), _user(cur, "p2@a.test")
    t = dt.time(14, 0)
    kw = dict(mmg_id=mmg, session_date=FUTURE, session_time=t,
              gm_user_ids=[1, None, None, None], allow_closed_slot=True)

    first = bookings.create_booking(cur, player_id=p1, **kw)
    assert first.capacity == 1
    with pytest.raises(BookingError, match="已經被預約"):
        bookings.create_booking(cur, player_id=p2, **kw)


def test_instant_store_rejects_capacity_above_one_on_save(shop):
    from admin_utils import mmg_admin
    from db.bookings import BookingError

    cur = shop("shop-a")
    _set_function(cur, "deposit", "off")
    mmg = _mmg(cur, waitlist_limit=1)
    data = {**mmg_admin.get(cur, mmg), "waitlist_limit": 2}
    with pytest.raises(BookingError, match="序位上限請填 1"):
        mmg_admin.update(cur, mmg, data)

    # 收訂金的店不受影響
    _set_function(cur, "deposit", "on")
    data["booking_cost"] = data["booking_cost_holiday"] = 0
    mmg_admin.update(cur, mmg, data)
    assert mmg_admin.get(cur, mmg)["waitlist_limit"] == 2


# ── 2. 改回「待主持確認」 ─────────────────────────────────────────

def test_back_to_gm_confirm_clears_confirmations_and_asks_again(shop):
    from admin_utils import bookings_admin
    from db import bookings, roles
    import notify_events

    cur = shop("shop-b")                      # shop-b 是 assign 模式
    _set_function(cur, "deposit", "on")
    cur.execute("UPDATE roles SET email_notification = true WHERE id = 2")
    roles.invalidate()
    mmg = _mmg(cur, booking_cost=500, booking_cost_holiday=500, gm1_name="主持")
    gm = _user(cur, "gm@b.test", role=2)
    player = _user(cur, "pl@b.test")
    bid = _booking(cur, mmg, player, FUTURE, dt.time(14, 0), "booked", gm=gm, confirmed=True)

    before = bookings.get_booking(cur, bid)
    # 前端送來的確認勾仍是 true（舊畫面），後端照樣要清
    bookings_admin.update(cur, bid, _admin_data(cur, bid, status="gm_confirm"))
    after = bookings.get_booking(cur, bid)
    assert after["status"] == "gm_confirm"
    assert _admin_data(cur, bid)["gm_confirmed"][0] is False

    # 測試環境沒設定 Brevo，包在外面的 @safe 會直接回 None；直接呼叫裡面那支。
    mails = [m for m in notify_events.on_transition.__wrapped__(cur, before, after) if m]
    assert [m.to[0]["email"] for m in mails if "重新確認" in m.subject] == ["gm@b.test"]

    # 之後只改備註，不會再自己跳回「待收訂金」
    bookings_admin.update(cur, bid, _admin_data(cur, bid, note="只改備註"))
    assert bookings.get_booking(cur, bid)["status"] == "gm_confirm"

    # 主持人重新確認 → 照正常流程往前走
    bookings.confirm_gm(cur, bid, gm)
    assert bookings.get_booking(cur, bid)["status"] == "gm_reviewed"


def test_new_booking_is_not_treated_as_sent_back(shop):
    """後台新增會套一次 update；那不是「改回去」，勾好的確認不能被清掉。"""
    from admin_utils import bookings_admin
    from db import bookings

    cur = shop("shop-b")
    _set_function(cur, "deposit", "on")
    mmg = _mmg(cur, booking_cost=500, booking_cost_holiday=500, gm1_name="主持", gm1_user=[])
    gm = _user(cur, "gm2@b.test", role=2)
    cur.execute("UPDATE mmg SET gm1_user = %s WHERE id = %s", ([gm], mmg))
    player = _user(cur, "pl2@b.test")
    bid = bookings_admin.create(cur, {
        "mmg_id": mmg, "player_id": player, "session_date": FUTURE,
        "session_time": dt.time(19, 0), "status": "gm_confirm",
        "gm_user_ids": [gm, None, None, None], "gm_confirmed": [True, False, False, False],
    })
    assert _admin_data(cur, bid)["gm_confirmed"][0] is True


# ── 3. 已取消改回來的序位預覽 ──────────────────────────────────────

def test_revive_preview_reports_who_moves_back(shop):
    from admin_utils import bookings_admin

    cur = shop("shop-b")
    mmg = _mmg(cur, gm1_name="主持")
    t = dt.time(10, 0)
    a, b, c = (_user(cur, f"{x}@rv.test") for x in "abc")
    base = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
    first = _booking(cur, mmg, a, FUTURE, t, "booked", created=base)
    revived = _booking(cur, mmg, b, FUTURE, t, "cancelled", created=base + dt.timedelta(hours=1))
    _booking(cur, mmg, c, FUTURE, t, "gm_confirm", created=base + dt.timedelta(hours=2))

    p = bookings_admin.revive_preview(cur, revived)
    assert p["position"] == 2
    assert [(m["from"], m["to"], m["player_name"]) for m in p["moved"]] == [(2, 3, "c")]
    assert p["booked"] == [{"position": 1, "player_name": "a"}]

    # 排在最後面的那一筆改回來：沒有人往後移
    cur.execute("UPDATE booking_record SET status = 'cancelled' WHERE id = %s", (first,))
    last = _booking(cur, mmg, a, FUTURE, t, "cancelled", created=base + dt.timedelta(hours=3))
    assert bookings_admin.revive_preview(cur, last)["moved"] == []


# ── 4. 下架、停用之前的「未來場次」 ────────────────────────────────

def test_future_bookings_lists_only_upcoming_live_ones(shop):
    from admin_utils import bookings_admin

    cur = shop("shop-b")
    mmg = _mmg(cur, gm1_name="主持")
    gm = _user(cur, "gm@fb.test", role=2)
    player = _user(cur, "pl@fb.test")
    t = dt.time(15, 0)
    live = _booking(cur, mmg, player, FUTURE, t, "booked", gm=gm)
    _booking(cur, mmg, player, FUTURE, dt.time(18, 0), "cancelled", gm=gm)   # 已取消
    _booking(cur, mmg, player, PAST, t, "booked", gm=gm)                     # 已經過了

    assert [b["id"] for b in bookings_admin.future_bookings(cur, mmg_id=mmg)["bookings"]] == [live]
    of_gm = bookings_admin.future_bookings(cur, user_id=gm)
    assert [b["id"] for b in of_gm["as_gm"]] == [live] and of_gm["as_player"] == []
    of_player = bookings_admin.future_bookings(cur, user_id=player)
    assert [b["id"] for b in of_player["as_player"]] == [live] and of_player["as_gm"] == []
