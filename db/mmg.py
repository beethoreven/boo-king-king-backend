"""劇本（mmg）的查詢。"""

from __future__ import annotations

from db import listing


def _row_to_dict(cur, row) -> dict:
    """把一列 mmg 展開成 dict，並把 gm{n}_user 的 id 換成人名。

    角色是「名稱 + 可帶的人」一組：gm{n}_name 為 NULL 代表這個劇本沒有
    這個角色，那一組就不會出現在回傳的 gm_slots 裡。
    """
    (mid, name, url, period, price, cost, players, wl, status,
     n1, u1, n2, u2, n3, u3, n4, u4, price_holiday, cost_holiday) = row

    slots = []
    for idx, (gname, guids) in enumerate(((n1, u1), (n2, u2), (n3, u3), (n4, u4)), start=1):
        if gname is None:
            continue
        gms = []
        if guids:
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
        "waitlist_limit": wl,
        "status": status,
        "gm_slots": slots,
    }


_COLUMNS = (
    "id, name, url, period, price, booking_cost, players, waitlist_limit, status, "
    "gm1_name, gm1_user, gm2_name, gm2_user, gm3_name, gm3_user, gm4_name, gm4_user, "
    "price_holiday, booking_cost_holiday"
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
    """單一劇本。不篩狀態——管理員與主持人也走這支。

    「看不到的劇本也訂不到」不是由這支負責，而是 create_booking 在建立
    預約的同一個交易裡自己查一次 listing.VISIBLE_SQL（見 db/bookings.py）。
    擋在那裡而不是這裡，是因為那個檢查必須跟搶位在同一個交易內才有意義——
    在別處先問過、再開一個交易去寫入，中間就有空隙。

    ★ 這裡曾經有一支 is_visible() 想當那個關卡，但從來沒有任何地方呼叫過
      （2026-09-01 刪除）。留著一個「看起來是關卡、實際上沒接線」的函式，
      比沒有更危險：下一個讀的人會以為呼叫它就安全了。
    """
    cur.execute(f"SELECT {_COLUMNS} FROM mmg WHERE id = %s", (mmg_id,))
    row = cur.fetchone()
    return _row_to_dict(cur, row) if row else None
