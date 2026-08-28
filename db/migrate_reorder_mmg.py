"""
一次性遷移：重建 mmg，讓欄位順序符合閱讀時的分組——

    id, name, room_id, period, ready_time_cost, reset_time_cost, ...

room_id 貼著 name（「這齣戲是什麼、在哪演」），三個時間欄位排在一起
（「演多久、前置多久、收拾多久」）。直接看資料表時不用左右來回捲。

    python3 -m db.migrate_reorder_mmg --dry-run
    python3 -m db.migrate_reorder_mmg

為什麼要重建整張表：Postgres 沒有調整既有欄位順序的語法。ALTER TABLE
ADD COLUMN 一律加在最後面，所以後來補上的 room_id / ready / reset 都
擠在尾巴。跟先前 mms→mmg、以及 booking_record 加 gm{n}_confirmed 時
是同一個問題、同一套做法。

★ 比前兩次多一道手續：booking_record.mmg_id 有外鍵指著這張表。外鍵跟的
  是表的 OID 不是名字，所以 RENAME 之後它會繼續指向改名後的舊表，讓
  DROP 失敗。必須先卸下那個外鍵，最後再指回新表。

跑完就可以刪掉這支檔案——全新建立的資料庫直接就是正確順序（見
schema.py）。
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from db.connection import pool  # noqa: E402

DESIRED = [
    "id", "name", "room_id",
    "period", "ready_time_cost", "reset_time_cost",
    "price", "booking_cost", "players", "waitlist_limit",
    "gm1_name", "gm1_user",
    "gm2_name", "gm2_user",
    "gm3_name", "gm3_user",
    "gm4_name", "gm4_user",
    "status",
]


def column_order(cur, table: str) -> list[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


def snapshot(cur) -> list[tuple]:
    """遷移前後各拍一次，用來確認資料一個字都沒變。"""
    cur.execute(f"SELECT {', '.join(DESIRED)} FROM mmg ORDER BY id")
    return cur.fetchall()


def migrate(cur) -> int:
    cols = ", ".join(DESIRED)

    # 1. 卸下指向 mmg 的外鍵。不卸的話它會跟著 RENAME 指向 mmg_old，
    #    最後 DROP mmg_old 會被擋下來。
    cur.execute(
        """
        SELECT tc.constraint_name
          FROM information_schema.table_constraints tc
          JOIN information_schema.constraint_column_usage ccu
            ON ccu.constraint_name = tc.constraint_name
         WHERE tc.constraint_type = 'FOREIGN KEY'
           AND tc.table_name = 'booking_record'
           AND ccu.table_name = 'mmg'
        """
    )
    fks = [r[0] for r in cur.fetchall()]
    for name in fks:
        cur.execute(f'ALTER TABLE booking_record DROP CONSTRAINT "{name}"')

    # 2. 舊表與序列改名讓路
    cur.execute("ALTER TABLE mmg RENAME TO mmg_old")
    cur.execute("ALTER SEQUENCE mmg_id_seq RENAME TO mmg_old_id_seq")

    # 3. 建新表。DDL 與 schema.py 的 _ensure_mmg 一致，只有順序不同——
    #    兩邊要一起改，不然新舊資料庫的結構會分岔。
    cur.execute(
        """
        CREATE TABLE mmg (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            room_id INTEGER NOT NULL DEFAULT 1 REFERENCES rooms(id),
            period NUMERIC(3, 1),
            ready_time_cost NUMERIC(3, 1) NOT NULL DEFAULT 0.5,
            reset_time_cost NUMERIC(3, 1) NOT NULL DEFAULT 0.5,
            price INTEGER,
            booking_cost INTEGER,
            players TEXT,
            waitlist_limit INTEGER NOT NULL DEFAULT 3 CHECK (waitlist_limit >= 0),
            gm1_name TEXT,
            gm1_user INTEGER[] NOT NULL DEFAULT '{}',
            gm2_name TEXT,
            gm2_user INTEGER[] NOT NULL DEFAULT '{}',
            gm3_name TEXT,
            gm3_user INTEGER[] NOT NULL DEFAULT '{}',
            gm4_name TEXT,
            gm4_user INTEGER[] NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'active'
        )
        """
    )

    # 4. 搬資料。兩邊都逐欄列名，不用 SELECT *——舊表的順序跟新表不同，
    #    靠位置對應必然錯位，而且錯得不會報錯（型別剛好相容時就默默塞進
    #    錯的欄位）。
    cur.execute(f"INSERT INTO mmg ({cols}) SELECT {cols} FROM mmg_old")
    moved = cur.rowcount

    # 5. 序列。手動帶 id 的 INSERT 不會推進序列，不補的話下一次新增會撞號。
    cur.execute("SELECT setval('mmg_id_seq', COALESCE((SELECT MAX(id) FROM mmg), 1))")

    # 6. 丟掉舊表，把外鍵指回新表
    cur.execute("DROP TABLE mmg_old")
    cur.execute("DROP SEQUENCE IF EXISTS mmg_old_id_seq")
    cur.execute(
        "ALTER TABLE booking_record ADD CONSTRAINT booking_record_mmg_id_fkey "
        "FOREIGN KEY (mmg_id) REFERENCES mmg(id)"
    )

    # 7. CHECK 約束（gm{n}_name 為空時 gm{n}_user 必須是空陣列）。
    #    ensure_schema 每次啟動也會補，這裡先建好是為了讓遷移跑完當下
    #    資料庫就是完整的，不必等下一次啟動。
    for n in (1, 2, 3, 4):
        cur.execute(
            f"ALTER TABLE mmg ADD CONSTRAINT gm{n}_slot_consistent "
            f"CHECK (gm{n}_name IS NOT NULL OR cardinality(gm{n}_user) = 0)"
        )
    return moved


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="做完所有事再回捲，不寫入")
    args = ap.parse_args()

    pool.open()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            before_order = column_order(cur, "mmg")
            before_rows = snapshot(cur)
            print(f"  遷移前欄位順序：{before_order}")
            print(f"  遷移前資料：{len(before_rows)} 筆")

            if before_order == DESIRED:
                print("\n  欄位順序已經正確，不需要遷移。")
                conn.rollback()
                return 0

            moved = migrate(cur)
            after_order = column_order(cur, "mmg")
            after_rows = snapshot(cur)

            print(f"\n  搬移 {moved} 筆")
            print(f"  遷移後欄位順序：{after_order}")

            ok = after_order == DESIRED and before_rows == after_rows
            if not ok:
                print("\n  ✗ 驗證失敗，回捲")
                if after_order != DESIRED:
                    print(f"    順序不符，期望 {DESIRED}")
                if before_rows != after_rows:
                    print("    資料內容有變動")
                conn.rollback()
                return 1

            print("  ✓ 順序正確，且每一列的每一欄都與遷移前相同")
            if args.dry_run:
                conn.rollback()
                print("\n  --dry-run：已回捲，資料庫未變動")
            else:
                conn.commit()
                print("\n  已提交")
    pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
