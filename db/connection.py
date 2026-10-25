"""
全站共用的資料庫連線層。每次呼叫各自開一條連線，用號誌限制同時上限。

    with pool.connection() as conn:
        with conn.cursor() as cur:
            ...

★ 連的是**目前這家店**的資料庫（tenancy.current()）。不在任何店家裡呼叫
  會拋 tenancy.NoTenant——不退回某個預設的店，理由見 tenancy.py。

## 為什麼不是連線池（2026-08-31 實測後改的）

原本用 psycopg3 的 ConnectionPool。它在本機完全正常，在 Render 的免費方案
上會**永久卡死**，而且是無聲的。

實際觀測到的：後端閒置幾分鐘後，之後每一次資料庫存取都在 20 秒後
`PoolTimeout`，連續兩次請求都一樣（所以不是冷啟動）。同一個行程裡繞過
池子直連只要 0.6 秒就成功——資料庫、網路、憑證全部沒問題。

池子的統計把原因指出來了：

    connections_num : 1      從頭到尾只成功建立過 1 條
    connections_lost: 1      那 1 條在 Neon 休眠時斷了
    pool_size       : 2      但池子認為自己有 2 條
    pool_available  : 0      可用 0 條
    requests_waiting: 1      有人在等，等了 20.6 秒

池子登記了「要再建一條」（size 1→2），但那個工作**從來沒有完成**
（connections_num 沒動），而且日誌裡**沒有任何連線失敗紀錄**——它不是
連了失敗，是根本沒去連。

原因是 Render 免費方案閒置時會凍結容器（它自己的橫幅寫著「will spin down
with inactivity, which can delay requests by 50 seconds or more」）。行程
本身活著（instance id 在整段失敗期間都沒變），主執行緒被喚醒後正常工作，
但**連線池賴以建立連線的背景執行緒沒有跟著恢復**。

這不是參數調得出來的。連線池的整個設計就是把建立連線交給背景執行緒——
那些執行緒在這個託管模型下沒有存活保證，池子就不可能可靠。

★ 姊妹專案 zh-cn-to-tw 在 Render 上踩過同一類問題並做了同樣的取捨，記在
  known-issue-check 清單的第 16、17、18 條。

## 代價與換到的東西

代價是每次請求多一次連線握手。同區實測約 0.2-0.6 秒——不是零，但現況是
**完全不能用**，這個代價划算得多。（機房必須同區，見下方。）

換到的是結構性的保證：沒有任何跨請求的共用狀態，所以沒有東西會「壞掉之後
一直壞著」。每一次請求自己開、自己關，前一次的失敗不會傳染給下一次。

## 多店家：兩層上限

一個服務同時服務好幾家店之後，上限分兩層：

  - 整台機器：同時最多 _MAX_CONCURRENT 條（保護 Render 那台機器，跟原本一樣）
  - 每一家店：同時最多 _MAX_PER_TENANT 條

第二層存在的理由是開賣搶位：一家店的尖峰把整台機器的名額用光，其他店
的玩家就連登入都卡住。每家店留不到全部，別家就永遠有名額可用。只有一家
店時兩層一樣大，行為跟原本完全相同。

★ 先拿店家那一層、再拿整台機器那一層。反過來的話，一家店塞車的請求會
  佔著機器名額排隊等店家名額，又變回大家一起卡住。

## ★ 號誌不能省

拿掉池子的同時也拿掉了「同時最多 5 條連線」這個上限——那是池子順便在做、
但沒有寫在它名字裡的事。少了它，尖峰時的連線數不再有天花板，而 Render
免費方案是 0.1 CPU / 512MB。

zh-cn-to-tw 就是這樣把服務打掛過：拿掉全域鎖之後，10 個併發請求讓整個
服務回 502 直到自己重啟。所以這裡用號誌補回那個上限，而且兩個方向都有界：
  - 同時最多 _MAX_CONCURRENT 條
  - 等不到名額最多等 _SLOT_WAIT 秒，然後明確失敗

沒有第二個界的話，塞車時號誌會退化成一個無限長的佇列，就變回原本那種
「大家一起卡住」的狀況。

## 同區是前提，不是選配

Render 與 Neon 都必須開在 ap-southeast-1（Singapore）。zh-cn-to-tw 實測，
握手成本幾乎完全由網路距離決定（連線耗時 ÷ 一次乾淨的 RTT ≈ 6，兩條不同
路徑分別量到 5.98 與 6.15）。跨區時單次查詢 1.76 秒、同區 0.03 秒，差 59 倍。

現在每次請求都要握手一次，這一點比用池子的時候更重要，不是更不重要。
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager

import psycopg

import tenancy
from db import idle_gap, schema

# 整台機器同時可以有幾條連線。這是反壓上限，不是效能旋鈕。
#
# 實際量到的尖峰約 5-6 條併發，而且不是來自很多使用者，是「一次輪詢展開成
# 好幾個帶授權的請求」。這個數字必須配合 Render 方案的實際 CPU/RAM 重新
# 驗證，不要假設不變——升級成付費機器（0.5 CPU）之後可以實測再調高，
# 用環境變數 DB_MAX_CONCURRENT 改，不用改程式。
_MAX_CONCURRENT = int(os.environ.get("DB_MAX_CONCURRENT", "5"))

# 每一家店同時可以有幾條（見上方「多店家：兩層上限」）。預設：只有一家店時
# 跟整台一樣大；好幾家時是 3，留 2 條給其他店。
_MAX_PER_TENANT = int(os.environ.get(
    "DB_MAX_CONCURRENT_PER_TENANT",
    str(_MAX_CONCURRENT if len(tenancy.all_tenants()) == 1 else min(3, _MAX_CONCURRENT)),
))
if not 1 <= _MAX_PER_TENANT <= _MAX_CONCURRENT:
    raise tenancy.ConfigError(
        f"DB_MAX_CONCURRENT_PER_TENANT={_MAX_PER_TENANT} 要介於 1 和 "
        f"DB_MAX_CONCURRENT={_MAX_CONCURRENT} 之間。"
    )

# 等不到名額最多等多久。超過就明確失敗，不要無限期排隊。
_SLOT_WAIT = 20

# 單次連線的握手上限。同區正常是 0.2-0.6 秒，10 秒代表真的有問題。
_CONNECT_TIMEOUT = 10

# 整台機器那一層刻意是全行程共用的：它保護的是這台機器，不是某家店。
_slots = threading.BoundedSemaphore(_MAX_CONCURRENT)
_tenant_slots = tenancy.PerTenant(lambda: threading.BoundedSemaphore(_MAX_PER_TENANT))


class DatabaseBusy(Exception):
    """同時連線數已達上限，而且等不到名額。"""


def _configure(conn) -> None:
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


class _Connections:
    """跟原本連線池同介面的替代品（pool.connection()），所以呼叫端一行都不用改。

    沒有 open() / close()：沒有需要預先建立、也沒有需要關閉的長駐資源，那正是
    這個做法的重點。（2026-10-25 之前留著兩個空的 open/close 給舊呼叫點相容，
    改多店家時一併拿掉了。）
    """

    def get_stats(self) -> dict:
        """給 /api/ready 的診斷用。"""
        return {
            "mode": "per-request",
            "max_concurrent": _MAX_CONCURRENT,
            "max_concurrent_per_tenant": _MAX_PER_TENANT,
            "slot_wait_seconds": _SLOT_WAIT,
            "connect_timeout_seconds": _CONNECT_TIMEOUT,
        }

    @contextmanager
    def connection(self, timeout: float | None = None):
        """開一條連線，用完就關。

        交易語意跟原本的連線池一致：正常離開就 commit，拋例外就 rollback
        （那是 `with conn:` 的行為）。差別只在最後會 close 而不是還回池子。

        ★ 這家店在這個行程裡第一次連線時，會先確認資料表都建好了（見
          db/schema.ensure_ready）。之後每次只是看一個旗標。
        """
        tenant = tenancy.current()

        # 在取得名額之前記錄，理由跟原本一樣：等名額等了 20 秒也是「這段
        # 期間有人在用資料庫」，算成沒有存取會低估密集度（見 db/idle_gap.py）。
        idle_gap.mark()

        wait = _SLOT_WAIT if timeout is None else timeout
        deadline = time.monotonic() + wait
        tenant_slots = _tenant_slots.get()
        if not tenant_slots.acquire(timeout=wait):
            raise DatabaseBusy(
                f"{tenant.id} 的資料庫同時連線已達上限（{_MAX_PER_TENANT}），"
                f"等待 {wait} 秒仍無法取得"
            )
        try:
            if not _slots.acquire(timeout=max(0.0, deadline - time.monotonic())):
                raise DatabaseBusy(
                    f"資料庫同時連線已達上限（整台 {_MAX_CONCURRENT}），"
                    f"等待 {wait} 秒仍無法取得"
                )
            try:
                conn = psycopg.connect(tenant.database_url, connect_timeout=_CONNECT_TIMEOUT)
                try:
                    _configure(conn)
                    schema.ensure_ready(conn)
                    with conn:
                        yield conn
                finally:
                    conn.close()
            finally:
                _slots.release()
        finally:
            tenant_slots.release()


pool = _Connections()
