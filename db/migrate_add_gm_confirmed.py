"""
一次性遷移：重建 booking_record，讓 gm{n}_confirmed 緊鄰對應的
gm{n}_user_id，並確保 status 的 CHECK（若未來加上）與應用層
VALID_STATUSES 都認得 'ended'。

    python3 -m db.migrate_add_gm_confirmed --dry-run
    python3 -m db.migrate_add_gm_confirmed

為什麼要重建整張表：Postgres 沒有調整既有欄位順序的語法（同一個問題
先前處理 mms→mmg 時也遇過），只能「建一張順序正確的新表 → 搬資料 →
換掉舊表」。這張表目前資料量很小，重建成本可以忽略；跑完就可以刪掉
這支檔案——全新建立的資料庫直接就是正確順序（見 schema.py）。
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from db.connection import pool  # noqa: E402

DESIRED = [
    "id", "mmg_id", "player_id", "created_at", "session_date", "session_time",
    "status", "deposit", "note",
    "gm1_user_id", "gm1_confirmed",
    "gm2_user_id", "gm2_confirmed",
    "gm3_user_id", "gm3_confirmed",
    "gm4_user_id", "gm4_confirmed",
    "request_id",
]

# 遷移前的舊欄位（不含 gm{n}_confirmed，那是這次要加的）
OLD_COLUMNS = [c for c in DESIRED if not c.endswith("_confirmed")]


def column_order(cur, table: str) -> list[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


def migrate(cur) -> int:
    new_cols = ", ".join(DESIRED)

    cur.execute("ALTER TABLE booking_record RENAME TO booking_record_old")
    cur.execute("ALTER SEQUENCE booking_record_id_seq RENAME TO booking_record_old_id_seq")

    cur.execute(
        """
        CREATE TABLE booking_record (
            id SERIAL PRIMARY KEY,
            mmg_id INTEGER NOT NULL REFERENCES mmg(id),
            player_id INTEGER NOT NULL REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            session_date DATE NOT NULL,
            session_time TIME NOT NULL,
            status TEXT NOT NULL DEFAULT 'gm_confirm',
            deposit INTEGER NOT NULL DEFAULT 0,
            note TEXT,
            gm1_user_id INTEGER REFERENCES users(id),
            gm1_confirmed BOOLEAN NOT NULL DEFAULT false,
            gm2_user_id INTEGER REFERENCES users(id),
            gm2_confirmed BOOLEAN NOT NULL DEFAULT false,
            gm3_user_id INTEGER REFERENCES users(id),
            gm3_confirmed BOOLEAN NOT NULL DEFAULT false,
            gm4_user_id INTEGER REFERENCES users(id),
            gm4_confirmed BOOLEAN NOT NULL DEFAULT false,
            request_id UUID NOT NULL UNIQUE
        )
        """
    )

    # SELECT 的每個欄位必須跟 INSERT 目標欄位「逐一對齊位置」，不能
    # 把四個 false 全部塞在最後面——那會讓 gm1_confirmed 收到
    # gm2_user_id 的值，跟 mms→mmg 遷移時提醒過的是同一種錯誤。
    # 新增的 gm{n}_confirmed 一律填 false（舊表沒有這個概念，一律視為
    # 尚未確認）。
    cur.execute(
        f"""
        INSERT INTO booking_record ({new_cols})
        SELECT id, mmg_id, player_id, created_at, session_date, session_time,
               status, deposit, note,
               gm1_user_id, false,
               gm2_user_id, false,
               gm3_user_id, false,
               gm4_user_id, false,
               request_id
          FROM booking_record_old
         ORDER BY id
        """  # noqa: S608
    )
    cur.execute("SELECT COUNT(*) FROM booking_record")
    moved = cur.fetchone()[0]

    cur.execute("SELECT COALESCE(MAX(id), 0) FROM booking_record")
    max_id = cur.fetchone()[0]
    if max_id > 0:
        cur.execute("SELECT setval('booking_record_id_seq', %s, true)", (max_id,))

    cur.execute("DROP TABLE booking_record_old")

    return moved, max_id


def main() -> int:
    parser = argparse.ArgumentParser(description="booking_record 加入 gm{n}_confirmed 並重排欄位")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    pool.open()
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                before = column_order(cur, "booking_record")
                if before == DESIRED:
                    print("已經是目標順序，不需要處理。")
                    return 0

                print("目前欄位順序：")
                print("   ", " → ".join(before))
                print("\n目標欄位順序：")
                print("   ", " → ".join(DESIRED))

                missing = [c for c in OLD_COLUMNS if c not in before]
                if missing:
                    print(f"\n✗ 缺少預期欄位，中止：{missing}")
                    return 1

                cur.execute("SELECT COUNT(*) FROM booking_record")
                print(f"\n將搬移 {cur.fetchone()[0]} 筆資料")

                if args.dry_run:
                    print("\n（--dry-run，未修改任何東西）")
                    return 0

                print("\n執行遷移：")
                moved, max_id = migrate(cur)
                conn.commit()
                print(f"    搬移 {moved} 筆，序列下一個 id = {max_id + 1}")

                after = column_order(cur, "booking_record")
                print("\n完成後欄位順序：")
                print("   ", " → ".join(after))
                ok = after == DESIRED
                print("\n結果：" + ("通過" if ok else "與預期不符，請檢查"))
                return 0 if ok else 1
    finally:
        pool.close()


if __name__ == "__main__":
    sys.exit(main())
