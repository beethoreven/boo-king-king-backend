"""劇本（mmg）的查詢。"""

from __future__ import annotations

from db import listing


def _row_to_dict(cur, row) -> dict:
    """把一列 mmg 展開成 dict，並把 gm{n}_user 的 id 換成人名。

    角色是「名稱 + 可帶的人」一組：gm{n}_name 為 NULL 代表這個劇本沒有
    這個角色，那一組就不會出現在回傳的 gm_slots 裡。
    """
    (mid, name, url, period, price, cost, players, wl, status,
     n1, u1, n2, u2, n3, u3, n4, u4) = row

    slots = []
    for idx, (gname, guids) in enumerate(((n1, u1), (n2, u2), (n3, u3), (n4, u4)), start=1):
        if gname is None:
            continue
        hosts = []
        if guids:
            cur.execute(
                "SELECT id, name FROM users "
                " WHERE id = ANY(%s) AND status = 'active' ORDER BY id",
                (guids,),
            )
            hosts = [{"id": i, "name": n} for i, n in cur.fetchall()]
        slots.append({"slot": idx, "name": gname, "hosts": hosts})

    return {
        "id": mid,
        "name": name,
        # 劇本簡介的外部連結，沒有就是 None。前端據此決定名稱要不要變成
        # 可以點的連結。
        "url": url,
        # NUMERIC 在 psycopg 是 Decimal，JSON 序列化不了，轉成 float。
        # 時長只有小數點後一位，float 的精度綽綽有餘。
        "period": float(period) if period is not None else None,
        "price": price,
        "booking_cost": cost,
        "players": players,
        "waitlist_limit": wl,
        "status": status,
        "gm_slots": slots,
    }


_COLUMNS = (
    "id, name, url, period, price, booking_cost, players, waitlist_limit, status, "
    "gm1_name, gm1_user, gm2_name, gm2_user, gm3_name, gm3_user, gm4_name, gm4_user"
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

    玩家路徑要擋的是「看不到的劇本也訂不到」，那由 is_visible 負責，
    在需要的端點各自呼叫，而不是讓這支自己吞掉不該顯示的資料。
    """
    cur.execute(f"SELECT {_COLUMNS} FROM mmg WHERE id = %s", (mmg_id,))
    row = cur.fetchone()
    return _row_to_dict(cur, row) if row else None


def is_visible(cur, mmg_id: int) -> bool:
    """這齣戲此刻是不是玩家看得到、訂得到的。"""
    now = listing.now_taipei(cur)
    cur.execute(
        f"SELECT EXISTS (SELECT 1 FROM mmg WHERE id = %(id)s AND {listing.VISIBLE_SQL})",
        {"id": mmg_id, "now": now},
    )
    return bool(cur.fetchone()[0])
