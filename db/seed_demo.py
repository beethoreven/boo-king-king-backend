"""
寫入一組示範資料，用來實際看看目前的 schema 長什麼樣子。

    python3 -m db.seed_demo          # 寫入並顯示
    python3 -m db.seed_demo --show   # 只顯示，不寫入

這支跟 verify_schema.py 的 --seed 不同：那邊塞的是拋棄式測試資料（前綴
boo-king-king-verify，--clean 會清掉），這裡放的是要留著看的示範內容。
兩者互不干擾。

可重複執行：所有寫入都是 ON CONFLICT DO UPDATE，跑第二次只會把欄位更新
成腳本裡的值，不會產生重複資料。
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import tenancy  # noqa: E402
from db.connection import pool  # noqa: E402
from db.schema import ensure_schema  # noqa: E402

USERS = [
    # (id, email, name, role)
    (1, "beethoreven@gmail.com", "阿舍老師", 1),
    (2, "arthur1.shen.pega@gmail.com", "Arthur", 2),
    (3, "arthur.shen.netflix@gmail.com", "網飛", 2),
]

# 《竊雲台》有三個主持人角色，沒有第四個。
# 每個角色目前各由一位主持人擔任，但 gm{n}_user 是陣列，之後要加人
# 直接往陣列裡塞 user id 即可，不用改結構。
MMG = {
    "id": 1,
    "name": "竊雲台",
    "period": 10,          # 遊戲時長 10 小時
    "price": 2000,
    "booking_cost": 1000,  # 訂金
    "players": "6",        # 純顯示字串
    "gm_names": ["柳送君", "朱懿辰", "九千歲", None],
    "gm_users": [[1], [2], [3], []],
    "status": "active",
}


def _print_header(title: str) -> None:
    print(f"\n{'=' * 64}\n  {title}\n{'=' * 64}")


def seed(cur) -> None:
    _print_header("寫入示範資料")

    for uid, email, name, role in USERS:
        cur.execute(
            """
            INSERT INTO users (id, email, name, role, status, line_id, phone)
            VALUES (%s, %s, %s, %s, 'active', NULL, NULL)
            ON CONFLICT (id) DO UPDATE
                SET email = EXCLUDED.email,
                    name  = EXCLUDED.name,
                    role  = EXCLUDED.role
            """,
            (uid, email, name, role),
        )
    print(f"    users  寫入 {len(USERS)} 筆")

    cur.execute(
        """
        INSERT INTO mmg (id, name, period, price, price_holiday,
                         booking_cost, booking_cost_holiday, players,
                         gm1_name, gm2_name, gm3_name, gm4_name,
                         gm1_user, gm2_user, gm3_user, gm4_user, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE
            SET name = EXCLUDED.name, period = EXCLUDED.period,
                price = EXCLUDED.price, price_holiday = EXCLUDED.price_holiday,
                booking_cost = EXCLUDED.booking_cost,
                booking_cost_holiday = EXCLUDED.booking_cost_holiday,
                players = EXCLUDED.players,
                gm1_name = EXCLUDED.gm1_name, gm2_name = EXCLUDED.gm2_name,
                gm3_name = EXCLUDED.gm3_name, gm4_name = EXCLUDED.gm4_name,
                gm1_user = EXCLUDED.gm1_user, gm2_user = EXCLUDED.gm2_user,
                gm3_user = EXCLUDED.gm3_user, gm4_user = EXCLUDED.gm4_user,
                status = EXCLUDED.status
        """,
        (
            # 示範劇本只有一種價錢：假日沿用平日的值。
            MMG["id"], MMG["name"], MMG["period"], MMG["price"], MMG["price"],
            MMG["booking_cost"], MMG["booking_cost"], MMG["players"],
            *MMG["gm_names"],
            *MMG["gm_users"],
            MMG["status"],
        ),
    )
    print("    mmg    寫入 1 筆")

    _fix_sequences(cur)


def _fix_sequences(cur) -> None:
    """把 SERIAL 序列推到目前最大 id 之後。

    這一步不能省。上面的 INSERT 明確指定了 id，而 Postgres 的 SERIAL 序列
    只有在「沒指定 id、讓它自動產生」時才會前進——手動塞 id=1,2,3 之後，
    序列仍然停在 0，下一次正常新增會拿到 id=1 而撞上既有資料，
    報 duplicate key violation。

    危險的是這個錯誤不會在寫入示範資料當下出現，要等到之後第一筆
    真實資料進來才爆。對「開賣前一週先把場次建好」這種流程來說，
    引爆點會剛好落在開賣當天的第一筆預約。
    """
    _print_header("修正序列值")
    for table in ("users", "mmg", "booking_record"):
        cur.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}")  # noqa: S608 - 常數表名
        max_id = cur.fetchone()[0]
        seq = f"{table}_id_seq"
        if max_id > 0:
            # is_called=true 代表 last_value 已經發出去了，下一個會是 max_id+1
            cur.execute("SELECT setval(%s, %s, true)", (seq, max_id))
            print(f"    {table:<16} 下一個 id 將是 {max_id + 1}")
        else:
            cur.execute("SELECT setval(%s, 1, false)", (seq,))
            print(f"    {table:<16} 無資料，下一個 id 將是 1")


def show(cur) -> None:
    _print_header("users")
    cur.execute("SELECT id, email, name, role, status, line_id, phone FROM users ORDER BY id")
    # 從 roles 表讀，不要在這裡再放一份——這支的用途正是「印出資料庫裡
    # 有什麼」，用一份自己的對照表印，印出來的就不一定是資料庫裡的東西。
    from db import roles as roles_db
    role_label = {rid: r["name"] for rid, r in roles_db.all_roles(cur).items()}
    for uid, email, name, role, status, line_id, phone in cur.fetchall():
        print(f"    id={uid}  {name:<8} {role_label.get(role, role):<8} {status:<8} {email}")
        print(f"           line_id={line_id or '(空)'}  phone={phone or '(空)'}")

    _print_header("mmg")
    cur.execute(
        """
        SELECT id, name, period, price, booking_cost, players, waitlist_limit, status,
               gm1_name, gm2_name, gm3_name, gm4_name,
               gm1_user, gm2_user, gm3_user, gm4_user
        FROM mmg ORDER BY id
        """
    )
    for row in cur.fetchall():
        (mid, name, period, price, cost, players, wl, status,
         *rest) = row
        gm_names = rest[0:4]
        gm_users = rest[4:8]
        print(f"    id={mid}  《{name}》  {status}")
        print(f"           時長 {period} 小時 ／ 售價 {price} ／ 訂金 {cost} ／ 人數 {players}")
        print(f"           序位上限 {wl} 組（第 1 序位之外）")
        print(f"    {'-' * 56}")
        for i, (gname, gusers) in enumerate(zip(gm_names, gm_users), start=1):
            if gname is None:
                print(f"           角色{i}  （此劇本沒有這個角色）")
                continue
            if gusers:
                cur.execute("SELECT id, name FROM users WHERE id = ANY(%s) ORDER BY id", (gusers,))
                who = "、".join(f"{n}(id={i2})" for i2, n in cur.fetchall())
            else:
                who = "（尚未指派）"
            print(f"           角色{i}  {gname:<8} → {who}")

    _print_header("booking_record")
    cur.execute("SELECT COUNT(*) FROM booking_record")
    n = cur.fetchone()[0]
    print(f"    目前 {n} 筆" + ("" if n else "（還沒有任何預約）"))


def main() -> int:
    parser = argparse.ArgumentParser(description="寫入 boo-king-king 示範資料")
    parser.add_argument("--show", action="store_true", help="只顯示現況，不寫入")
    args = parser.parse_args()

    # 有好幾家店時要用 TENANT=<店家> 指定（見 tenancy.for_cli）。
    tenant = tenancy.for_cli()
    url = tenant.database_url
    print(f"店家：{tenant.id}　連線目標：{url.split('@')[-1].split('/')[0] if '@' in url else '(未設定)'}")

    pool.open()
    try:
        with tenancy.use(tenant), pool.connection() as conn:
            with conn.cursor() as cur:
                ensure_schema(cur)
                if not args.show:
                    seed(cur)
                conn.commit()
                show(cur)
        print()
        return 0
    finally:
        pool.close()


if __name__ == "__main__":
    sys.exit(main())
