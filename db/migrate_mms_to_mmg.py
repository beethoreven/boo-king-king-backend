"""
一次性遷移：把 mms 改名為 mmg，同時把欄位重排成 gm{n}_name 與 gm{n}_user 相鄰。

    python3 -m db.migrate_mms_to_mmg --dry-run   # 只檢查，不動資料
    python3 -m db.migrate_mms_to_mmg             # 實際執行

為什麼兩件事合在一起做：
改名本身很便宜（ALTER TABLE ... RENAME TO 只改中繼資料），但重排欄位不行——
Postgres 沒有調整既有欄位順序的語法，唯一辦法是「建一張順序正確的新表 →
搬資料 → 換掉舊表」。既然無論如何都要重建，就在重建時直接用新名字建，
比「先改名、再重建」少一次搬運。

booking_record.mms_id 也一併改名為 mmg_id。留著舊欄位名指向新表會是那種
最容易讓人看錯的不一致。

安全性：整段在一個交易裡，任何一步失敗都會完整回捲。已經遷移過時會直接
跳過，重複執行無害。跑完就可以刪掉這支檔案——全新建立的資料庫直接就是
正確的名稱與順序（見 schema.py）。
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from db.connection import pool  # noqa: E402

# 目標順序。gm{n}_name 與 gm{n}_user 成對相鄰，status 收尾。
DESIRED = [
    "id", "name", "period", "price", "booking_cost", "players", "waitlist_limit",
    "gm1_name", "gm1_user",
    "gm2_name", "gm2_user",
    "gm3_name", "gm3_user",
    "gm4_name", "gm4_user",
    "status",
]


def table_exists(cur, name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = %s",
        (name,),
    )
    return cur.fetchone() is not None


def column_order(cur, table: str) -> list[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


def migrate(cur) -> int:
    cols = ", ".join(DESIRED)

    # 檢視表會擋住 DROP TABLE（它們依賴 mms），先清掉。
    # 這兩個是先前為了瀏覽方便加的，已決定不保留。
    cur.execute("DROP VIEW IF EXISTS v_booking")
    cur.execute("DROP VIEW IF EXISTS v_mms")
    cur.execute("DROP VIEW IF EXISTS v_mmg")

    # 1. 用目標名稱與順序建新表。這份定義刻意與 schema.py 重複——
    #    遷移腳本應該凍結在「執行當下」的樣子，跟著 schema.py 一起演化
    #    會讓已經跑過的歷史遷移行為改變。
    cur.execute(
        """
        CREATE TABLE mmg (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            period NUMERIC(3, 1),
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

    # 2. 搬資料。明確列出欄位名，不要用 SELECT *——新舊表順序不同，
    #    靠位置對應會把值塞進錯誤的欄位。
    cur.execute(f"INSERT INTO mmg ({cols}) SELECT {cols} FROM mms")  # noqa: S608
    cur.execute("SELECT COUNT(*) FROM mmg")
    moved = cur.fetchone()[0]

    # 3. 序列推到正確位置。新序列從 1 開始，但 id 是搬過來的，
    #    不推的話下一筆新增會撞號。
    cur.execute("SELECT COALESCE(MAX(id), 0) FROM mmg")
    max_id = cur.fetchone()[0]
    if max_id > 0:
        cur.execute("SELECT setval('mmg_id_seq', %s, true)", (max_id,))

    # 4. CHECK 約束屬於舊表，不會跟著資料搬過來，要重建。
    for n in (1, 2, 3, 4):
        cur.execute(
            f"""
            ALTER TABLE mmg ADD CONSTRAINT gm{n}_slot_consistent
            CHECK (gm{n}_name IS NOT NULL OR cardinality(gm{n}_user) = 0)
            """
        )

    # 5. booking_record 的外鍵欄位改名並改指新表。
    #    索引會自動跟著欄位改名更新，不用手動處理。
    cur.execute("ALTER TABLE booking_record DROP CONSTRAINT booking_record_mms_id_fkey")
    cur.execute("ALTER TABLE booking_record RENAME COLUMN mms_id TO mmg_id")
    cur.execute(
        "ALTER TABLE booking_record ADD CONSTRAINT booking_record_mmg_id_fkey "
        "FOREIGN KEY (mmg_id) REFERENCES mmg(id)"
    )

    # 6. 舊表退場。不用 CASCADE——若還有東西依賴它，讓它報錯比靜靜連帶刪掉安全。
    cur.execute("DROP TABLE mms")

    return moved, max_id


def main() -> int:
    parser = argparse.ArgumentParser(description="mms → mmg 改名並重排欄位")
    parser.add_argument("--dry-run", action="store_true", help="只檢查，不修改")
    args = parser.parse_args()

    pool.open()
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                has_old = table_exists(cur, "mms")
                has_new = table_exists(cur, "mmg")

                if has_new and not has_old:
                    order = column_order(cur, "mmg")
                    print("mmg 已存在、mms 已移除——先前已完成遷移。")
                    print("目前欄位順序：")
                    print("   ", " → ".join(order))
                    print("\n結果：" + ("符合目標" if order == DESIRED else "順序與目標不符"))
                    return 0 if order == DESIRED else 1

                if not has_old:
                    print("找不到 mms 資料表，也沒有 mmg，無事可做。")
                    return 1

                before = column_order(cur, "mms")
                print("目前 mms 欄位順序：")
                print("   ", " → ".join(before))
                print("\n目標 mmg 欄位順序：")
                print("   ", " → ".join(DESIRED))

                missing = [c for c in DESIRED if c not in before]
                extra = [c for c in before if c not in DESIRED]
                if missing or extra:
                    print(f"\n✗ 欄位對不上，中止。缺少={missing} 多出={extra}")
                    return 1

                cur.execute("SELECT COUNT(*) FROM mms")
                print(f"\n將搬移 {cur.fetchone()[0]} 筆資料")
                cur.execute("SELECT COUNT(*) FROM booking_record")
                print(f"booking_record 有 {cur.fetchone()[0]} 筆，其 mms_id 將改名為 mmg_id")

                if args.dry_run:
                    print("\n（--dry-run，未修改任何東西）")
                    return 0

                print("\n執行遷移：")
                moved, max_id = migrate(cur)
                conn.commit()
                print(f"    搬移 {moved} 筆，mmg 序列下一個 id = {max_id + 1}")

                after = column_order(cur, "mmg")
                print("\n完成後 mmg 欄位順序：")
                print("   ", " → ".join(after))
                ok = after == DESIRED and not table_exists(cur, "mms")
                print("\n結果：" + ("通過" if ok else "與預期不符，請檢查"))
                return 0 if ok else 1
    finally:
        pool.close()


if __name__ == "__main__":
    sys.exit(main())
