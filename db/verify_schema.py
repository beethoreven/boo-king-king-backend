"""
連上真正的 Neon 資料庫，跑一次 ensure_schema()，然後把實際建出來的東西
印出來核對。

用途是「我改完 schema.py，想確認它在真的 Postgres 上跑得起來、而且長成
我以為的樣子」——不是單元測試，是給人看的檢查工具。

    python3 -m db.verify_schema

預設只讀不寫（除了建表本身）。要順便塞幾筆範例資料進去看看關聯對不對，
加 --seed：

    python3 -m db.verify_schema --seed

--seed 塞的資料都用 boo-king-king-verify 開頭的 email 標記，可以用
--clean 全部清掉，不會誤刪真實資料：

    python3 -m db.verify_schema --clean
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

# 一定要在 import db.connection 之前載入 .env——那支模組在 import 當下就會
# 讀 os.environ["DATABASE_URL"]，晚一步就會 KeyError。
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import tenancy  # noqa: E402
from db.connection import pool  # noqa: E402
from db.schema import ensure_schema  # noqa: E402

# 範例資料一律用這個前綴，--clean 靠它辨識該刪哪些，不會碰到真實帳號。
SEED_PREFIX = "boo-king-king-verify"

EXPECTED_TABLES = ["users", "mmg", "booking_record"]


def _print_header(title: str) -> None:
    print(f"\n{'=' * 62}\n  {title}\n{'=' * 62}")


def show_structure(cur) -> bool:
    """印出四張表的欄位與索引，回傳是否全部都在。"""
    ok = True

    cur.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = ANY(%s)
        """,
        (EXPECTED_TABLES,),
    )
    found = {row[0] for row in cur.fetchall()}
    missing = [t for t in EXPECTED_TABLES if t not in found]
    if missing:
        print(f"  ✗ 缺少資料表：{', '.join(missing)}")
        ok = False

    for table in EXPECTED_TABLES:
        if table not in found:
            continue
        _print_header(f"{table}")
        cur.execute(
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table,),
        )
        for name, dtype, nullable, default in cur.fetchall():
            null_mark = "" if nullable == "YES" else "  NOT NULL"
            # 預設值可能很長（例如 nextval(...)），截短以免洗版
            default_mark = ""
            if default:
                short = default if len(default) <= 30 else default[:27] + "..."
                default_mark = f"  DEFAULT {short}"
            print(f"    {name:<16} {dtype:<26}{null_mark}{default_mark}")

        cur.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = %s"
            " ORDER BY indexname",
            (table,),
        )
        indexes = [row[0] for row in cur.fetchall()]
        if indexes:
            print(f"    {'—' * 52}")
            for idx in indexes:
                print(f"    索引  {idx}")

    return ok


def show_counts(cur) -> None:
    _print_header("目前資料筆數")
    for table in EXPECTED_TABLES:
        cur.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 - 表名來自本檔常數，非外部輸入
        print(f"    {table:<16} {cur.fetchone()[0]:>6} 筆")


def seed(cur) -> None:
    """塞一組互相關聯的範例資料，用來確認外鍵與序位計算真的能運作。

    情境：一個需要兩位主持人的劇本，第一個角色有兩位主持人可選；
    三組玩家搶同一個時段——依先後順序分別是第 1、2、3 序位。
    """
    _print_header("寫入範例資料")

    # 主持人兩位、玩家三位
    user_ids = {}
    for label, role in [
        ("gm-alice", 2),
        ("gm-bob", 2),
        ("player-1", 3),
        ("player-2", 3),
        ("player-3", 3),
    ]:
        cur.execute(
            """
            INSERT INTO users (email, name, role, line_id, phone)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
            """,
            (f"{SEED_PREFIX}-{label}@example.com", label, role, f"line_{label}", "0900000000"),
        )
        user_ids[label] = cur.fetchone()[0]
    print(f"    users        新增/更新 {len(user_ids)} 筆")

    # 一個需要兩位主持人的劇本。
    # 角色 1 有兩位主持人可選、角色 2 只有一位、角色 3/4 這個劇本沒有。
    cur.execute(
        """
        INSERT INTO mmg (name, period, price, price_holiday, booking_cost,
                         booking_cost_holiday, players,
                         gm1_name, gm2_name, gm1_user, gm2_user, waitlist_limit)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            f"{SEED_PREFIX} 範例劇本", 3.5, 1200, 1200, 300, 300, "6-8 人",
            "主控", "NPC",
            [user_ids["gm-alice"], user_ids["gm-bob"]],
            [user_ids["gm-alice"]],
            3,
        ),
    )
    mmg_id = cur.fetchone()[0]
    print(f"    mmg          新增 1 筆 (id={mmg_id}, 時長 3.5h, 訂金 300)")
    print("                 角色1 可選 2 位主持人、角色2 可選 1 位、角色3/4 不存在")

    # 三組玩家搶同一個時段。created_at 用 DEFAULT NOW()，插入先後即順位。
    for label in ["player-1", "player-2", "player-3"]:
        cur.execute(
            """
            INSERT INTO booking_record
                (mmg_id, player_id, session_date, session_time, day_type, deposit,
                 gm1_user_id, gm2_user_id, request_id)
            VALUES (%s, %s, DATE '2026-09-05', TIME '14:00', 'holiday', 300, %s, %s,
                    gen_random_uuid())
            """,
            (mmg_id, user_ids[label], user_ids["gm-alice"], user_ids["gm-alice"]),
        )
    print("    booking_record 新增 3 筆 (同一時段，測試序位計算)")

    return mmg_id


def show_positions(cur) -> None:
    """用實際資料驗證序位的算法（schema.py 裡記載的那段查詢）。"""
    _print_header("序位驗算")
    cur.execute(
        """
        SELECT b.session_date, b.session_time, u.name, b.status,
               ROW_NUMBER() OVER (
                   PARTITION BY b.mmg_id, b.session_date, b.session_time
                   ORDER BY b.created_at
               ) AS position
        FROM booking_record b
        JOIN users u ON u.id = b.player_id
        JOIN mmg m ON m.id = b.mmg_id
        WHERE m.name LIKE %s AND b.status <> 'cancelled'
        ORDER BY b.session_date, b.session_time, position
        """,
        (f"{SEED_PREFIX}%",),
    )
    rows = cur.fetchall()
    if not rows:
        print("    （沒有範例資料，用 --seed 產生）")
        return
    for date, time, name, status, position in rows:
        print(f"    {date} {time}  第 {position} 序位  {name:<12} {status}")


def check_gm_constraint(cur) -> bool:
    """確認「沒有這個角色就不能指派主持人」的 CHECK 約束真的會擋。

    刻意寫入一筆矛盾資料（gm3_name 為空、卻指派了 gm3_user），預期被資料庫
    拒絕。如果它竟然寫進去了，代表約束沒建起來，這比沒驗證更危險——
    會讓人以為有保護。用 SAVEPOINT 包起來，失敗後回捲不影響外層交易。
    """
    _print_header("約束驗證：沒有的角色不能指派主持人")
    cur.execute("SAVEPOINT constraint_probe")
    try:
        cur.execute(
            """
            INSERT INTO mmg (name, gm1_name, gm3_user) VALUES (%s, %s, %s)
            """,
            (f"{SEED_PREFIX} 違規測試", "主控", [1]),
        )
    except Exception as exc:  # noqa: BLE001 - 這裡預期會失敗，型別不重要
        cur.execute("ROLLBACK TO SAVEPOINT constraint_probe")
        name = type(exc).__name__
        print(f"    ✓ 寫入被拒絕（{name}）——gm3_name 為空時不接受 gm3_user")
        return True
    cur.execute("ROLLBACK TO SAVEPOINT constraint_probe")
    print("    ✗ 矛盾資料竟然寫入成功——CHECK 約束沒有生效")
    return False


def clean(cur) -> None:
    _print_header("清除範例資料")
    # 先刪參照方（booking_record / mms_gm），再刪被參照方，否則外鍵會擋。
    cur.execute(
        "DELETE FROM booking_record WHERE mmg_id IN (SELECT id FROM mmg WHERE name LIKE %s)",
        (f"{SEED_PREFIX}%",),
    )
    print(f"    booking_record 刪除 {cur.rowcount} 筆")
    cur.execute("DELETE FROM mmg WHERE name LIKE %s", (f"{SEED_PREFIX}%",))
    print(f"    mmg            刪除 {cur.rowcount} 筆")
    cur.execute("DELETE FROM users WHERE email LIKE %s", (f"{SEED_PREFIX}%",))
    print(f"    users          刪除 {cur.rowcount} 筆")


def check_sequences(cur) -> bool:
    """確認每個 serial 的序列值沒有落後於實際最大 id。

    落後的話，下一次 INSERT 會撞 duplicate key。這在資料是用匯入/還原
    方式進去的時候很常見（序列不會跟著資料一起前進），而且要等到
    第一筆真實寫入才會爆——對開賣前一週才建好場次資料的流程來說，
    那個引爆點正好會落在開賣當天。所以每次驗證都檢查一遍。
    """
    _print_header("序列值檢查")
    ok = True
    for table in EXPECTED_TABLES:
        cur.execute(
            "SELECT pg_get_serial_sequence(%s, 'id')",
            (table,),
        )
        seq = cur.fetchone()[0]
        if not seq:
            continue
        cur.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}")  # noqa: S608 - 表名為本檔常數
        max_id = cur.fetchone()[0]
        # 直接查序列本身，不要查 pg_sequences——後者沒有 is_called 欄位，
        # 而少了它就分不出「last_value=1 且還沒發過號」與「已經發過 1 號」，
        # 兩者的下一個 id 分別是 1 和 2。seq 來自 pg_get_serial_sequence()
        # 對本檔常數表名的查詢結果，不是外部輸入。
        cur.execute(f"SELECT last_value, is_called FROM {seq}")  # noqa: S608
        last_value, is_called = cur.fetchone()
        # is_called = False 代表序列還沒被用過，下一次會發 last_value 本身
        next_val = last_value if not is_called else last_value + 1
        if next_val <= max_id:
            print(f"    ✗ {table:<16} 下一個 id={next_val} 但已有 MAX(id)={max_id} → 會撞號")
            ok = False
        else:
            print(f"    ✓ {table:<16} MAX(id)={max_id}, 下一個 id={next_val}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="驗證 boo-king-king 的資料庫 schema")
    parser.add_argument("--seed", action="store_true", help="寫入範例資料")
    parser.add_argument("--clean", action="store_true", help="清除範例資料後結束")
    args = parser.parse_args()

    # 有好幾家店時要用 TENANT=<店家> 指定（見 tenancy.for_cli）。
    tenant = tenancy.for_cli()
    host = tenant.database_url
    # 只印 host 段，不要把密碼印到終端機或 CI log 裡
    shown = host.split("@")[-1].split("/")[0] if "@" in host else "(未設定)"
    print(f"店家：{tenant.id}　連線目標：{shown}")

    with tenancy.use(tenant), pool.connection() as conn:
        with conn.cursor() as cur:
            ensure_schema(cur)
            conn.commit()

            if args.clean:
                clean(cur)
                conn.commit()
                return 0

            structure_ok = show_structure(cur)

            if args.seed:
                seed(cur)
                conn.commit()

            show_counts(cur)
            show_positions(cur)
            constraint_ok = check_gm_constraint(cur)
            sequences_ok = check_sequences(cur)
            conn.commit()

    print()
    if structure_ok and sequences_ok and constraint_ok:
        print("結果：通過")
        return 0
    print("結果：有問題，見上面標示 ✗ 的項目")
    return 1


if __name__ == "__main__":
    sys.exit(main())
