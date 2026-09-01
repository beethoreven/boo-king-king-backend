"""濫用防護：每個帳號的速率限制，與異常請求的紀錄。

## 為什麼是「每個帳號」而不是全站

全站限制會讓一個人的行為影響到所有人——開賣尖峰三十個人同時搶，正好是
最不該有人被擋下的時刻。按帳號算的話，某個人打爆自己的額度不影響別人。

## 累進停權（2026-09-01 加）

原本只記錄不處置。缺點是一個持續在洗 API 的帳號可以無限期洗下去，而
記下來的東西沒有任何後果。

現在改成累進制：每產生一列新的 abuse_log 就給使用者加點（見
POINTS_BY_KIND），累計點數對照 abuse_rule 表決定停權多久。

停權的代價仍然是不對稱的——被停權的人當場被登出、訂不了場、只能打電話
找店家。所以三件事一起做，來壓低誤判的傷害：

  1. **限流門檻從 120 提高到 240。** 實測顯示 120 貼著真實使用（見下方
     RATE_LIMIT_PER_MINUTE 的註解）。
  2. **兩種訊號分開計分。** rate_limit 是弱訊號只給 1 點，要三次相隔五
     分鐘以上的違規才會碰到最輕的停權。
  3. **點數每週遞減。** 偶爾誤觸的人會自己降回 0，只有累犯才會往上爬。
     沒有這一條的話，老客人一年下來必然被封（見 jobs.py 的每週遞減）。

管理員在編輯畫面把狀態改回「啟用」就解除停權，同時點數歸零——不歸零的話
下一次違規會立刻跳回門檻以上，管理員會覺得解了個寂寞。

## 為什麼管理員永遠豁免

如果唯一的管理員被系統自己鎖掉，就沒有人能解鎖了。
"""

from __future__ import annotations

import threading
import time
from collections import deque

# ── 可調參數 ────────────────────────────────────────────────

# 每個帳號每分鐘的請求上限。正常使用大約是這個的二十分之一——量過的
# 數字：完整走一次預約流程 4 次、管理員存一筆劇本 1 次、切換頁籤 1 次。
# 訂在 120 是為了讓「連續重整」這種真人行為離門檻還很遠。
# 每個帳號每分鐘的請求上限。
#
# 2026-09-01 從 120 提高到 240。理由是實測：一個正常玩家在開賣時找位子，
# 光是換劇本（2 次）、翻日曆月份（每個沒看過的月份 1 次）、查時段（1 次）
# 就很容易在一分鐘內累積上百次呼叫——120 貼著真實使用，而現在超過門檻
# 不再只是記一筆，是會累積點數並導致停權，誤判的代價變高了。
RATE_LIMIT_PER_MINUTE = 240
RATE_WINDOW_SECONDS = 60

# 同一位使用者、同一種事件在這個窗口內重複發生時，累加既有那一列的
# hits，不插新列。被打爆的時候，寫紀錄本身不該變成第二個壓力來源。
MERGE_WINDOW_SECONDS = 300

# ★ 打開之後，壞請求累積到門檻就會自動停權。先跑一段時間確認記到的
#   都是該記的人，再打開。
# 每產生一列新的 abuse_log 要加幾點。
#
# ★ 兩種訊號的強度差很多，所以不同分：
#     bad_request  強。前端根本送不出格式錯誤的請求——那是有人手工在戳 API。
#     rate_limit   弱。開賣尖峰、網路重試、瘋狂重整都會觸發，正常玩家也會中。
#
# ★ 記點的單位是「一列 abuse_log」，不是「一個被擋下的請求」。被限流代表
#   已經送了門檻以上的請求，若每個被擋的請求都加分，同一秒就會直接跳到
#   停權門檻。abuse_log 本來就把同一人同一種事件在 MERGE_WINDOW_SECONDS
#   內併成一列，所以「一次違規」＝「一列」是現成而且合理的錨點：
#   一次爆發算一點，持續洗的話每 5 分鐘再一點。
POINTS_BY_KIND = {
    "bad_request": 2,
    "rate_limit": 1,
}
BAD_REQUEST_STRIKE_LIMIT = 20
STRIKE_WINDOW_SECONDS = 600

KIND_BAD_REQUEST = "bad_request"
KIND_RATE_LIMIT = "rate_limit"

# ── 速率限制（記憶體）────────────────────────────────────────
#
# 刻意不進資料庫：這一層每個請求都會經過，為了省資源而每次寫一筆，
# 是花更多資源。Render 休眠會讓計數歸零，但那對速率限制無所謂——
# 重來一個窗口而已，不是安全漏洞。

_hits: dict[int, deque] = {}
_hits_lock = threading.Lock()


def _prune(dq: deque, now: float) -> None:
    while dq and now - dq[0] > RATE_WINDOW_SECONDS:
        dq.popleft()


def check_rate(user_id: int) -> int | None:
    """記一次請求。超過上限時回傳建議的重試秒數，否則 None。"""
    now = time.monotonic()
    with _hits_lock:
        dq = _hits.setdefault(user_id, deque())
        _prune(dq, now)
        if len(dq) >= RATE_LIMIT_PER_MINUTE:
            # 最舊那一筆離開窗口就有額度了。
            return max(1, int(RATE_WINDOW_SECONDS - (now - dq[0])) + 1)
        dq.append(now)
        return None


def reset_rate(user_id: int | None = None) -> None:
    """清掉速率計數。測試與人工排除用。"""
    with _hits_lock:
        if user_id is None:
            _hits.clear()
        else:
            _hits.pop(user_id, None)


# ── 事件紀錄（資料庫）────────────────────────────────────────


def record(cur, user_id: int, kind: str, path: str | None, detail: str | None) -> dict | None:
    """寫一筆異常紀錄，短時間內的重複事件併進同一列。

    回傳 add_point() 的結果（見那支），或 None——併進既有那一列時不加點，
    沒有加點就沒有值得通知的事。呼叫端用這個回傳決定要不要寄通知信。

    合併的鍵是 (user_id, kind)，不含 path：一個人亂試的時候通常會換著
    不同的端點打，每支各自開一列反而看不出「這個人在戳」這件事。path
    留最後一次的，detail 也是——要看細節就看最新那次，要看規模就看 hits。

    ★ 先下 advisory lock 再做「找不到就新增」。沒有這道鎖時，一波併發
      湧進來會有好幾個執行緒同時查到「沒有既有的列」而各自 INSERT——
      實測 48 次併發會開出 4 列。hits 總數仍然正確，但管理員看到的是
      「45 次」旁邊掛著三個「1 次」，那一頁就是拿來一眼判斷規模的，
      拆成四列等於把它最主要的用途弄壞。
      鎖的鍵只到 (user_id, kind)，擋的是同一個人同一種事件，不會互相
      影響——而這正是併發最兇的地方（被限速時每個執行緒都想記一筆）。
    """
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        (f"abuse|{user_id}|{kind}",),
    )
    cur.execute(
        """
        UPDATE abuse_log
           SET hits = hits + 1, last_at = NOW(), path = %s, detail = %s
         WHERE id = (
             SELECT id FROM abuse_log
              WHERE user_id = %s AND kind = %s
                AND last_at > NOW() - (%s::int * interval '1 second')
              ORDER BY last_at DESC LIMIT 1
         )
        """,
        (path, detail, user_id, kind, MERGE_WINDOW_SECONDS),
    )
    if cur.rowcount == 0:
        cur.execute(
            "INSERT INTO abuse_log (user_id, kind, path, detail) VALUES (%s, %s, %s, %s)",
            (user_id, kind, path, detail),
        )
        # ★ 只有「開了新的一列」才加點——這正是「又犯了一次」的定義。
        #   落在合併窗口內的重複事件只把 hits 加一，不加點。
        return add_point(cur, user_id, kind)
    return None


def add_point(cur, user_id: int, kind: str) -> dict | None:
    """給使用者加點，並依 abuse_rule 判斷要不要停權。

    回傳 {'added', 'total', 'ban'}：這次加了幾點、累計幾點，以及這次是否
    觸發停權（觸發時 ban 是 {'points', 'ban_seconds', 'label', 'until'}，
    否則 None）。使用者不存在才回 None。

    ★ 「有沒有加點」與「有沒有停權」是兩件不同的事，所以分成兩層而不是用
      「回 None 代表沒事」——通知信要能分辨「他被記了一點」跟「他被停權了」，
      這兩封信的收件人反應完全不同。

    ★ 呼叫端要負責清 session 與權限快取——那兩件事需要 import
      auth_utils.sessions 與 whitelist，而它們會 import 回這裡，直接在這
      呼叫會變成循環相依。所以這支只負責「算」，善後交給 app.py。
    """
    points = POINTS_BY_KIND.get(kind, 1)
    cur.execute(
        "UPDATE users SET abuse_point = abuse_point + %s WHERE id = %s "
        "RETURNING abuse_point, status",
        (points, user_id),
    )
    row = cur.fetchone()
    if row is None:
        return None
    total, status = row

    # 取「門檻 <= 目前點數」之中最高的那一條。找不到就是還沒到最輕的門檻。
    cur.execute(
        "SELECT min_point, ban_seconds, label FROM abuse_rule "
        "WHERE min_point <= %s ORDER BY min_point DESC LIMIT 1",
        (total,),
    )
    rule = cur.fetchone()
    if rule is None:
        return {"added": points, "total": total, "ban": None}
    _min_point, ban_seconds, label = rule

    # ban_seconds = 0 代表無限期，要管理員手動解。用 NULL 表示「沒有到期」，
    # 判定那邊會把 NULL 當成永遠有效（見 whitelist 的 effective status）。
    if ban_seconds > 0:
        cur.execute(
            "UPDATE users SET status = 'banned', "
            "banned_until = NOW() + (%s::int * interval '1 second') "
            "WHERE id = %s RETURNING banned_until",
            (ban_seconds, user_id),
        )
    else:
        cur.execute(
            "UPDATE users SET status = 'banned', banned_until = NULL "
            "WHERE id = %s RETURNING banned_until",
            (user_id,),
        )
    until = cur.fetchone()[0]
    return {
        "added": points,
        "total": total,
        "ban": {
            "points": total, "ban_seconds": ban_seconds,
            "label": label, "until": until,
        },
    }


def decay_points(cur, amount: int = 1) -> int:
    """每週遞減：所有人的點數減 amount，不會低於 0。回傳有幾個人被減。

    ★ 沒有這一條的話，每個人終將被封——點數只增不減，老客人偶爾誤觸幾次，
      累積幾年必然踩到門檻。遞減讓「偶爾」自己歸零，只有累犯會往上爬。
      做法參考早年 PTT 的水桶制。

    ★ 順便把已經到期的停權落地。判定本身是即時算的（到期就等於沒被停權），
      這裡只是讓資料庫裡的狀態跟事實一致，好讓管理員在使用者清單上看到
      對的東西。同一招見 db/listing.py。
    """
    cur.execute(
        "UPDATE users SET abuse_point = GREATEST(abuse_point - %s, 0) "
        "WHERE abuse_point > 0",
        (amount,),
    )
    decayed = cur.rowcount
    cur.execute(
        "UPDATE users SET status = 'active', banned_until = NULL "
        "WHERE status = 'banned' AND banned_until IS NOT NULL AND banned_until <= NOW()"
    )
    return decayed


def clear_points(cur, user_id: int) -> None:
    """點數歸零並解除停權。管理員把狀態改回「啟用」時呼叫。

    ★ 一定要跟解除停權綁在一起。只改狀態不清點數的話，對方下一次違規會
      立刻跳回門檻以上被重新停權，管理員會覺得解了個寂寞。
    """
    cur.execute(
        "UPDATE users SET abuse_point = 0, banned_until = NULL WHERE id = %s",
        (user_id,),
    )


def recent_bad_request_count(cur, user_id: int) -> int:
    """這個帳號在觀察窗口內累積的壞請求次數。"""
    cur.execute(
        """
        SELECT COALESCE(SUM(hits), 0) FROM abuse_log
         WHERE user_id = %s AND kind = %s
           AND last_at > NOW() - (%s::int * interval '1 second')
        """,
        (user_id, KIND_BAD_REQUEST, STRIKE_WINDOW_SECONDS),
    )
    return int(cur.fetchone()[0])


def list_log(cur, *, user_id: int | None = None, start: int = 1, limit: int = 50) -> dict:
    """異常紀錄清單，新的在前。管理員介面用。"""
    where = "WHERE a.user_id = %(user)s" if user_id else ""
    cur.execute(
        f"""
        SELECT a.id, a.user_id, u.email, u.name, a.kind, a.path, a.detail,
               a.hits, a.first_at, a.last_at
          FROM abuse_log a JOIN users u ON u.id = a.user_id
          {where}
         ORDER BY a.last_at DESC
         LIMIT %(limit)s OFFSET %(offset)s
        """,
        {"user": user_id, "limit": limit + 1, "offset": start - 1},
    )
    rows = cur.fetchall()
    has_more = len(rows) > limit
    return {
        "items": [
            {
                "id": r[0], "user_id": r[1], "email": r[2], "name": r[3],
                "kind": r[4], "path": r[5], "detail": r[6], "hits": r[7],
                "first_at": r[8].isoformat(), "last_at": r[9].isoformat(),
            }
            for r in rows[:limit]
        ],
        "has_more": has_more,
    }


def clear_log(cur, *, user_id: int | None = None) -> int:
    """清除紀錄。不指定使用者就是全部清掉。

    可以刪是刻意的：這張表是給人看的線索，不是稽核軌跡。處理完一個誤判
    之後留著只會讓下次查詢多一堆雜訊。真的需要不可刪除的稽核紀錄時，
    那是另一件事，不該跟這張表混在一起。
    """
    if user_id is None:
        cur.execute("DELETE FROM abuse_log")
    else:
        cur.execute("DELETE FROM abuse_log WHERE user_id = %s", (user_id,))
    return cur.rowcount
