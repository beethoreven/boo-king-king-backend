"""劇本（mmg）的查詢。"""

from __future__ import annotations

from db import functions, listing


def _row_to_dict(cur, row) -> dict:
    """把一列 mmg 展開成 dict，並把 gm{n}_user 的 id 換成人名。

    角色是「名稱 + 可帶的人」一組：gm{n}_name 為 NULL 代表這個劇本沒有
    這個角色，那一組就不會出現在回傳的 gm_slots 裡。

    ★ single 模式下 gms 一律回空的。這是玩家看得到的回應，而那種店的
      玩家沒有「挑主持人」這個動作——主持人是系統依組態填的。名單送出去
      只是把店裡誰是主持人這件事丟給每一個瀏覽劇本清單的人，而且前端
      根本沒有地方用它（理由同 mmg_admin.list_gm_candidates）。
    """
    single = functions.gm_mode(cur) == "single"
    (mid, name, url, period, price, cost, players, wl, status,
     n1, u1, n2, u2, n3, u3, n4, u4, price_holiday, cost_holiday, extra_note) = row

    slots = []
    for idx, (gname, guids) in enumerate(((n1, u1), (n2, u2), (n3, u3), (n4, u4)), start=1):
        if gname is None:
            continue
        gms = []
        if guids and not single:
            cur.execute(
                "SELECT id, name FROM users "
                " WHERE id = ANY(%s) AND status = 'active' ORDER BY id",
                (guids,),
            )
            gms = [{"id": i, "name": n} for i, n in cur.fetchall()]
        slots.append({"slot": idx, "name": gname, "gms": gms})

    return {
        "id": mid,
        "name": name,
        # 劇本簡介的外部連結，沒有就是 None。前端據此決定名稱要不要變成
        # 可以點的連結。
        "url": url,
        # NUMERIC 在 psycopg 是 Decimal，JSON 序列化不了，轉成 float。
        # 時長只有小數點後一位，float 的精度綽綽有餘。
        "period": float(period) if period is not None else None,
        # 沒有後綴的是平日。兩個一樣時畫面只顯示一個；只填一個就只顯示
        # 那一個（前端 ui.js 的 dualPrice）。
        "price": price,
        "price_holiday": price_holiday,
        "booking_cost": cost,
        "booking_cost_holiday": cost_holiday,
        "players": players,
        # 額外註記，沒有就是 None。前端放進那一排小標籤，None 就不顯示。
        "extra_note": extra_note,
        "waitlist_limit": wl,
        "status": status,
        "gm_slots": slots,
    }


_COLUMNS = (
    "id, name, url, period, price, booking_cost, players, waitlist_limit, status, "
    "gm1_name, gm1_user, gm2_name, gm2_user, gm3_name, gm3_user, gm4_name, gm4_user, "
    "price_holiday, booking_cost_holiday, extra_note"
)


def list_active(cur) -> list[dict]:
    """玩家看得到的劇本。

    ★ 用 listing.VISIBLE_SQL 而不是 `status = 'active'`：上架時刻已經到了、
      但欄位值還沒被落地的劇本也算數。判定用算的，不等排程，所以切換是
      準時到秒的——理由見 db/listing.py。
      「尚未上架」與「已下架」都不會出現在這裡，連選單裡都沒有。
    """
    now = listing.now_taipei(cur)
    cur.execute(
        f"SELECT {_COLUMNS} FROM mmg WHERE {listing.VISIBLE_SQL} ORDER BY id",
        {"now": now},
    )
    return [_row_to_dict(cur, r) for r in cur.fetchall()]


def get(cur, mmg_id: int) -> dict | None:
    """玩家看得到的單一劇本：「尚未上架」與「已下架」的都當作不存在，回 None。

    用在預約畫面的三支（劇本細節、可預約的日期、時段狀況），條件跟清單
    （list_active）同一段 listing.VISIBLE_SQL。

    ★ 少了這個條件，任何登入的人換個編號就讀得到還沒開賣、或已經下架的劇本的
      名稱、價錢與主持人——清單裡沒有它們，不代表打不到。管理員看全部劇本走的是
      /api/admin/mmg，不經過這裡。

    「看不到的劇本也訂不到」仍然由 create_booking 在建立預約的同一個交易裡自己
    查一次 listing.VISIBLE_SQL（見 db/bookings.py）。那個檢查必須跟搶位在同一個
    交易內才有意義——在別處先問過、再開一個交易去寫入，中間就有空隙。這裡的條件
    管的是「讀不讀得到」，不是那個關卡。
    """
    now = listing.now_taipei(cur)
    cur.execute(
        f"SELECT {_COLUMNS} FROM mmg WHERE id = %(id)s AND {listing.VISIBLE_SQL}",
        {"id": mmg_id, "now": now},
    )
    row = cur.fetchone()
    return _row_to_dict(cur, row) if row else None
