"""
全站共用的資料庫連線層。psycopg3 + ConnectionPool，原生慣例：
    with pool.connection() as conn:
        with conn.cursor() as cur:
            ...

這裡的每一個參數都是跟 zh-cn-to-tw 專案三輪實測交換之後定案的，不是預設值，
改動前請先讀懂下面的理由，否則很容易改回一個看起來更保守、實際上更危險的設定。

同區是前提，不是選配：Render 與 Neon 都必須開在 ap-southeast-1（Singapore）。
zh-cn-to-tw 的實測顯示，握手/查詢成本幾乎完全由網路距離決定
（connect 的耗時 ÷ 一次乾淨的 RTT ≈ 6，兩條不同路徑分別量到 5.98 與 6.15，
TCP 1 + TLS 1-2 + SCRAM 約 3 個往返）。跨區時光是 pool 的健檢
（check_connection）就要 236ms，同區只要 4.3ms——也就是說跨區時這裡的
每一個保護機制都會變得很貴，容易被為了效能而關掉，而那正是讓 pool
安全的東西。同區不是讓 pool 變得多餘，而是讓一個設定正確的 pool
第一次變得可行。
"""

from __future__ import annotations

import os
from contextlib import contextmanager

from psycopg_pool import ConnectionPool

from db import idle_gap

DATABASE_URL = os.environ["DATABASE_URL"]


def _configure(conn):
    """每條新連線建立後跑一次。

    把 idle_in_transaction_session_timeout 從 Postgres 預設的 5 分鐘收緊到
    10 秒。搶位交易的目標長度是個位數到幾十毫秒（lock → 檢查名額 →
    insert → commit，全部本地操作，不等外部任何東西）。5 分鐘的預設值
    對這個目標形同不存在——尖峰時 30+ 併發搶同一批座位，一個卡住的交易
    握著座位列鎖，等於所有人一起卡住，而預設值允許這個災難持續 5 分鐘。
    10 秒讓它變成一筆快速失敗的請求，而不是安靜地卡住尖峰。

    這個時限只保護「意外卡住」，不能拿來當作業務邏輯的一部分——任何會
    等待外部事物（付款、使用者確認、外部 API）的流程都不該放進交易，
    應該拆成「短交易建立暫留 → 交易外處理 → 短交易轉正」的兩段式。
    """
    with conn.cursor() as cur:
        cur.execute("SET idle_in_transaction_session_timeout = '10s'")
    conn.commit()


class _MeasuredPool(ConnectionPool):
    """在每次取用連線時記一筆時間戳，其餘行為與 ConnectionPool 完全相同。

    掛在這一層而不是 HTTP 層，是因為要量的是「Neon 的 compute 被碰到的
    頻率」。被快取擋掉的請求不會碰到資料庫，算進去會低估間隔。
    詳見 db/idle_gap.py。

    mark() 只碰記憶體裡的幾個數字，且刻意放在取用連線「之前」——放在
    後面的話，等連線等了 20 秒（尖峰排隊）會被算成那 20 秒沒有存取。
    """

    @contextmanager
    def connection(self, timeout=None):
        idle_gap.mark()
        with super().connection(timeout) as conn:
            yield conn


pool = _MeasuredPool(
    conninfo=DATABASE_URL,

    # 尖峰是 30+ 併發搶同一批座位，這是反壓上限，不是效能旋鈕。
    # 這個數字必須配合 Render 方案的實際 CPU/RAM 重新驗證，不要假設不變。
    max_size=5,

    # 等不到連線就明確失敗，不無限期排隊。
    timeout=20,

    kwargs={"connect_timeout": 10},

    # 發連線前先驗活。Neon 免費/付費方案都會在閒置時單方面斷開連線
    # （免費方案 5 分鐘 suspend 無法關閉），沒有健檢就會把死連線發出去，
    # 症狀是查詢時才報錯、跟 pool 本身無關，容易被誤判成「pool 又壞了」。
    check=ConnectionPool.check_connection,

    # 必須是 0。psycopg3 的 pool 會主動維持 min_size——背景 worker 發現
    # 連線數低於下限就會補建。如果 Neon 在免費方案，這會形成「休眠→
    # pool 補建→喚醒→再休眠」的迴圈,等於一個全天候的資料庫保活器,
    # 而免費方案每月只有 100 CU-hours(約 0.25 CU 跑 400 小時,且會依
    # 負載自動放大到 2 CU,放大時額度消耗最多快 8 倍)。24/7 保活會在
    # 月中把額度燒光,之後 compute 停用到下個帳期——這是會停機的額度
    # 問題,比多走一次健檢嚴重得多。
    min_size=0,

    # 連線回收要早於 Neon 的休眠門檻,讓 pool 主動收掉即將被伺服器端
    # 砍斷的連線,而不是等它變成死連線才靠 check 發現。
    max_idle=180,

    configure=_configure,

    # 不要在 import 時就連線——這支模組可能在 app 啟動最上面就被間接
    # import,時間點比 .env 載入還早的話,連線失敗會讓整個 process 起
    # 不來。呼叫端在真正需要時自行 pool.open() 或直接用 pool.connection()。
    open=False,
)
