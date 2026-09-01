"""`/api/mmg/<id>/slot` 的回應快取。純記憶體，沒有額外的服務。

## 這是拿來擋什麼

開賣尖峰真正的負載不是預約筆數，是「還有沒有位子」被重複整理。實測的
形態是：一分鐘內成立 30-36 筆預約，但同一分鐘的讀取是好幾百到上千次。
兩者差一到兩個數量級，而每一次讀取都要開一條連線、跑四個查詢，並且把
Neon 從休眠叫醒（每月只有 100 CU-hours）。

擋掉的是那些讀取。寫入路徑一步都不動——精準性由 Postgres 的唯一性約束
保證，跟這裡無關。

## 為什麼不是 Redis

原本規劃用 Redis，2026-09-01 改成行程內快取。理由是這個用途下 Redis
幾乎沒有多換到東西：

  跨 worker 共用    對**限流**是必要的（120 變成 240 就是規則壞了），
                    對**快取**只影響命中率——兩個 worker 各自 miss 一次，
                    不會有任何東西算錯。
  命中率            共用一份 ~96%，每 worker 一份 ~92%。差四個百分點。
  失效紀律的成本    **完全一樣**。Redis 沒有讓那件事變便宜。

換到的四個百分點，要付的是多一個服務、每家店一份實例、一組連線失敗的
處理、以及「Redis 重啟了」這個情境。不划算。

★ 同一個專案裡已經有一份同樣形狀的東西在跑：`auth_utils/whitelist.py`
  用字典 + TTL + 鎖 + 明確失效，快取「這個帳號還有效嗎」。

## 失效是粗粒度的，而且刻意如此

任何預約寫入都清空**整份**快取，不是只清那一個時段。

看起來浪費，但算一下不是：尖峰一分鐘 1000 次讀取配 36 次寫入，等於每
一「代」快取平均服務 28 次讀取、只有第一次未命中——命中率仍然是 96%，
跟精準失效沒有差別。因為**讀取遠多於寫入，粗粒度的代價自動被攤平**。

換到的是正確性上的保證：不需要推理「這次寫入該清哪些鍵」。而那個推理
在這裡特別容易錯，因為 `has_room_conflict` 是**跨劇本、跨時間區間**的
——A 劇本 10:00 開一場 12 小時的戲，會讓同一間包廂裡 B 劇本從 10:00 到
22:30 的每一個時段都變成「撞期」。精準失效要算出那整個區間裡的所有
劇本與時段；清空不用。

★ 未來新增任何會改預約的路徑，都自動被涵蓋——只要它走 db/bookings.py
  裡那幾支寫入函式，失效就已經在裡面了，不必記得在呼叫端補一行。

## 期限不是可選的

`_TTL_SECONDS` 是最後一道防線，擋的是失效擋不住的那些：

  1. 經典競態：讀取未命中 → 去資料庫拿到值 → **就在這個瞬間**有人寫入
     並清空快取（但快取本來就是空的，清了個寂寞）→ 讀取這時才把過期的
     值寫進快取。沒有後續的寫入會來清它。
  2. 不經過預約寫入、但會改變答案的東西：排期規則改動、劇本的
     `waitlist_limit` 或名稱被編輯。那些是管理員的低頻操作，用期限吸收
     比在每個編輯路徑上補失效便宜得多。

期限把「錯到有人發現為止」壓成「最多錯 N 秒」。設 0 等於整個關掉快取，
那是出事時的第一個開關。

## 這裡不放什麼

★ 回應裡沒有任何跟使用者有關的欄位（只有 mmg_id、名稱、日期時間、容量、
  已排隊組數、幾個布林值），所以跨使用者共用是安全的。排隊清單與撞期
  細節是**刻意不回傳**的，見 app.py 裡 get_slot 的註解——真要放進來，
  這份快取就必須改成 per-user，那時整件事的效益就沒了。
"""

from __future__ import annotations

import os
import threading
import time

# 一筆快取活多久。3 秒是在「省下多少查詢」與「最多錯多久」之間取的：
#
# 開賣那一分鐘值大約每 2 秒變一次，所以再長也沒有意義；而縮短的代價
# 比直覺小——1000 次讀取分佈在 60 秒裡，3 秒期限約 20 次未命中、1 秒
# 期限約 60 次，命中率從 98% 掉到 94%。**縮短期限幾乎不損失效益**，
# 所以有疑慮時往短的設。
#
# ★ 設成 0 就是整個停用（get 永遠 miss）。出事時的第一個開關。
_TTL_SECONDS = float(os.environ.get("SLOT_CACHE_TTL_SECONDS", "3"))

# (mmg_id, 日期 ISO, 時間 HH:MM) -> (到期的 monotonic 時間, 回應 dict)
_cache: dict[tuple[int, str, str], tuple[float, dict]] = {}
_lock = threading.Lock()

# 診斷用的計數。命中率是「這件事到底值不值得做」的唯一答案，
# 不量的話就只能繼續用猜的。
_hits = 0
_misses = 0
_clears = 0


def enabled() -> bool:
    return _TTL_SECONDS > 0


def key(mmg_id: int, session_date, session_time) -> tuple[int, str, str]:
    """把三個值正規化成鍵。

    日期與時間一律轉成字串再當鍵：呼叫端拿到的可能是 date/time 物件、
    也可能是字串，兩種混用會讓同一個時段變成兩個鍵——那不會報錯，只會
    讓命中率安靜地砍半。
    """
    d = session_date if isinstance(session_date, str) else session_date.isoformat()
    t = session_time if isinstance(session_time, str) else session_time.strftime("%H:%M")
    return (mmg_id, d, t)


def get(k: tuple[int, str, str]) -> dict | None:
    """取一筆還沒過期的快取。沒有就回 None。"""
    global _hits, _misses
    if _TTL_SECONDS <= 0:
        return None
    now = time.monotonic()
    with _lock:
        hit = _cache.get(k)
        if hit is not None and hit[0] > now:
            _hits += 1
            # 回複本。呼叫端拿去改（例如補上欄位）不該污染快取裡那一份。
            return dict(hit[1])
        _misses += 1
        return None


def put(k: tuple[int, str, str], value: dict) -> None:
    """放一筆進去。"""
    if _TTL_SECONDS <= 0:
        return
    with _lock:
        _cache[k] = (time.monotonic() + _TTL_SECONDS, dict(value))


def clear() -> None:
    """清空整份快取。任何預約寫入都要呼叫——理由見檔案開頭。

    ★ 這支被呼叫的位置在寫入函式**裡面**，也就是在交易 commit 之前。
      清空到 commit 之間有一個很短的窗口，落在那裡的讀取會快取到寫入前
      的值。那正是上面說的競態，由期限收尾——把失效搬到呼叫端的
      commit 之後可以縮小窗口，但代價是每個呼叫端都要記得補一行，
      而漏掉一個的後果比這個窗口嚴重得多。
    """
    global _clears
    with _lock:
        _cache.clear()
        _clears += 1


def stats() -> dict:
    """命中率等統計。★ 這支不可以碰資料庫（跟 idle_gap.stats 同理）。"""
    with _lock:
        total = _hits + _misses
        return {
            "enabled": _TTL_SECONDS > 0,
            "ttl_seconds": _TTL_SECONDS,
            "entries": len(_cache),
            "hits": _hits,
            "misses": _misses,
            "hit_rate": round(_hits / total, 3) if total else None,
            "clears": _clears,
            "pid": os.getpid(),
            "note": (
                "per-process：多 worker 時每個 worker 各有一份快取，"
                "這裡的命中率是該 worker 自己的。全站命中率會略低於"
                "單一 worker 的觀測值。"
            ),
        }
