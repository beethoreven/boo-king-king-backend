"""劇本的上架狀態，以及到點自動切換。

## 三個狀態

    preparing  尚未上架 —— 資料建好了，但還沒開放預約
    active     上架     —— 玩家看得到、訂得到
    inactive   下架     —— 玩家看不到。**只有管理員手動設得了**

★ end_booking（預約終止時間）到了**不會**把狀態改成 inactive。那個欄位
  管的是「還能不能預約」，不是「這齣戲還在不在架上」——有的店家只是不再
  接這齣的預約，並沒有要把它收起來。時間過了之後玩家訂不到（由
  VISIBLE_SQL 負責），但狀態仍然是 active。

★ 玩家的劇本清單只列 active。preparing 與 inactive 都不會出現，
  連選單裡都沒有——見 db/mmg.py 的 list_active。

★ 欄位值沿用既有的 inactive（案主口語說「deactive」，那是 users 表的
  用字）。兩張表用不同的字是既成事實，改名要動遷移又沒有換到任何東西，
  所以維持原樣；顯示層一律講「下架」。

## 到點自動切換：為什麼不是每分鐘跑一次 cron

這是本專案最貴的一個決定，理由跟功能無關，跟錢有關。

Neon 免費方案閒置 5 分鐘就讓 compute 休眠，每月額度 100 CU-hours。
一個每分鐘醒來查一次資料庫的排程，等於一天 1440 次存取、永遠不讓它睡——
量測過的估計是這樣就會從 30-45 CU-hours 跳到 90+，貼著上限。
換句話說，為了讓一齣戲準時上架而付出的代價，是整個資料庫在月底停機。
（背景見 db/idle_gap.py。）

所以做法是**惰性判定 + 順帶落地**：

  1. 判定用算的，不用等排程。玩家的清單查詢與 slot 查詢都直接把
     「preparing 但時間已到」算成上架、「end_booking 已過」算成不可預約。
     這一層是精準到秒的——時間一到，下一個請求看到的就是新狀態，
     不存在「排程還沒跑到」的空窗。

  2. 欄位值順帶補上。管理員打開劇本清單時跑一次 sync_status()，
     每日 job 也跑一次。這只是讓資料庫裡的字跟事實一致，好讓管理員
     看到的狀態是對的；它不影響玩家看到什麼，晚幾分鐘落地沒有差別。

結果是：使用者感受到的切換是準時的，而資料庫在沒有人使用時仍然睡得著。
真的要一個「準點」的排程時（例如未來要在上架瞬間發推播），那時再談
外部排程器；那是通知的需求，不是狀態的需求。
"""

from __future__ import annotations

from db import slot_cache

# 玩家看得到的狀態條件。惰性判定的核心，三個地方共用同一段 SQL：
# 劇本清單、單一劇本查詢、slot 查詢。各自寫一份就會有一份先過期。
#
# %(now)s 一律傳台北時間的當下（見 now_taipei）。
VISIBLE_SQL = """
    (
        (status = 'active' OR (status = 'preparing' AND start_booking <= %(now)s))
        -- 預約終止時間。過了就訂不到，但**不會**改變 status——
        -- 見上方說明，那是刻意的。
        AND (end_booking IS NULL OR end_booking > %(now)s)
    )
"""


def now_taipei(cur):
    """資料庫眼中的「台北此刻」。

    ★ 用資料庫的時間，不是 Python 的。兩邊時鐘可能有差，而判定與寫入
      如果各用一個時間來源，就會出現「查詢說還沒到、UPDATE 說已經過了」
      這種自相矛盾的結果。全系統的裸時間值一律以台北解讀。
    """
    cur.execute("SELECT (NOW() AT TIME ZONE 'Asia/Taipei')")
    return cur.fetchone()[0]


def sync_status(cur) -> dict:
    """把到點的劇本狀態落地。回傳這次改了哪些。

    只做「上架」這一半。end_booking 到了不會改狀態，理由見下方。
    """
    cur.execute(
        """
        UPDATE mmg SET status = 'active'
         WHERE status = 'preparing'
           AND start_booking <= (NOW() AT TIME ZONE 'Asia/Taipei')
        RETURNING id, name
        """
    )
    listed = [{"id": r[0], "name": r[1]} for r in cur.fetchall()]
    # 真的有劇本上架了才清。這一支被管理員的劇本清單（GET）與每日排程
    # 呼叫，絕大多數時候一列都沒改——那時候清等於白白讓所有人重查一次
    # （2026-09-13 補）。
    if listed:
        slot_cache.clear()

    # ★ 沒有「自動下架」這回事，這是刻意的（2026-09-01 拿掉的）。
    #
    #   end_booking 是「預約終止時間」，不是「下架時間」——有的店家不打算
    #   把戲收起來，只是不再接這齣的預約。系統把它標成「下架」是在替店家
    #   做一個他沒做的決定，而那個標籤之後會出現在他自己的清單上。
    #
    #   時間一過的效果由 VISIBLE_SQL 那個條件負責（玩家端訂不到），status
    #   維持原樣。要真的下架仍然做得到：管理員自己把狀態改成「下架」。
    return {"listed": listed, "delisted": []}
