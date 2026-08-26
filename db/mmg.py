"""劇本（mmg）的查詢。"""

from __future__ import annotations


def _row_to_dict(cur, row) -> dict:
    """把一列 mmg 展開成 dict，並把 gm{n}_user 的 id 換成人名。

    角色是「名稱 + 可帶的人」一組：gm{n}_name 為 NULL 代表這個劇本沒有
    這個角色，那一組就不會出現在回傳的 gm_slots 裡。
    """
    (mid, name, period, price, cost, players, wl, status,
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
    "id, name, period, price, booking_cost, players, waitlist_limit, status, "
    "gm1_name, gm1_user, gm2_name, gm2_user, gm3_name, gm3_user, gm4_name, gm4_user"
)


def list_active(cur) -> list[dict]:
    """可預約的劇本。停用的不回傳——玩家不該看到已下架的劇本。"""
    cur.execute(f"SELECT {_COLUMNS} FROM mmg WHERE status = 'active' ORDER BY id")
    return [_row_to_dict(cur, r) for r in cur.fetchall()]


def get(cur, mmg_id: int) -> dict | None:
    cur.execute(f"SELECT {_COLUMNS} FROM mmg WHERE id = %s", (mmg_id,))
    row = cur.fetchone()
    return _row_to_dict(cur, row) if row else None
