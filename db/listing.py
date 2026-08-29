"""劇本的上架狀態，以及到點自動切換。

## 三個狀態

    preparing  尚未上架 —— 資料建好了，但還沒開賣
    active     上架     —— 玩家看得到、訂得到
    inactive   下架     —— 玩家看不到

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
     「preparing 但時間已到」算成上架、「end_booking 已過」算成下架。
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

# 玩家看得到的狀態條件。惰性判定的核心，三個地方共用同一段 SQL：
# 劇本清單、單一劇本查詢、slot 查詢。各自寫一份就會有一份先過期。
#
# %(now)s 一律傳台北時間的當下（見 now_taipei）。
VISIBLE_SQL = """
    (
        (status = 'active' OR (status = 'preparing' AND start_booking <= %(now)s))
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

    先上架再下架，順序不能反：一齣上下架時間都已經過去的戲，最終應該是
    下架。先跑下架再跑上架的話，它會被下架、然後又被上架回來。
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

    cur.execute(
        """
        UPDATE mmg SET status = 'inactive'
         WHERE status IN ('preparing', 'active')
           AND end_booking IS NOT NULL
           AND end_booking <= (NOW() AT TIME ZONE 'Asia/Taipei')
        RETURNING id, name
        """
    )
    delisted = [{"id": r[0], "name": r[1]} for r in cur.fetchall()]

    return {"listed": listed, "delisted": delisted}
