"""量測「連續兩次真的碰到資料庫之間隔了多久」。

## 這是拿來回答哪個問題

Neon 免費方案閒置 5 分鐘就會讓 compute 休眠，每月額度 100 CU-hours。
營業時段內的最長無人存取間隔有沒有超過 5 分鐘，直接決定成本落在哪一邊：

    超過 → compute 會睡，約 30-45 CU-hours/月，免費方案很寬鬆
    沒超過 → 開店到打烊一路醒著，90+ CU-hours/月，貼著上限

沒有真實店家的流量就算不出來。所以量測要先埋著，等第一家店接進來之後
直接有數字可看，而不是那時候才開始等一個月。

## 三個刻意的設計

★ 絕對不寫資料庫。每次請求往 Postgres 記一筆存取紀錄，會讓 compute
  一直醒著——那會直接摧毀這裡要量的東西。純記憶體。

★ 埋在「真的取用連線」那一層，不是 HTTP 層。被快取擋掉的請求
  （whitelist 有 30 秒快取）不會吵醒 Neon，算進去會高估存取密度、
  低估間隔，剛好往「看起來比較貴」的方向錯。

★ 讀取這份統計的端點不可以碰資料庫。否則每查一次就自己製造一次存取，
  把間隔壓短、順便叫醒 compute——量測行為污染被量測的對象。

## 怎麼讀這些數字

統計是 per-process 的，重新部署或重啟就歸零。跑多個 worker 時請求會被
分散，每個 worker 看到的間隔都會比真實的全域間隔「長」：

    真實全域最長間隔 ≤ 任一 worker 觀察到的最長間隔

也就是說多 worker 時這是個上界，方向偏保守（傾向讓你以為會休眠）。
單 worker 時才是精確值。輸出裡附上 worker 數就是為了不要被誤讀。
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

# 分佈的分界（秒）。5 分鐘那一刀是 Neon 的休眠門檻，是這整份統計的重點：
# 落在 5 分鐘以上的間隔，每一個都代表 compute 有機會睡掉。
_BUCKETS = [
    ("<1m", 60),
    ("1-5m", 300),
    ("5-15m", 900),
    (">15m", float("inf")),
]

_lock = threading.Lock()
_last: float | None = None
_max_gap = 0.0
_count = 0
_hist = {name: 0 for name, _ in _BUCKETS}
_started_at = datetime.now(timezone.utc)
_started_mono = time.monotonic()


def mark() -> None:
    """記一次「即將取用資料庫連線」。

    用 monotonic 而不是牆上時間：系統校時、日光節約、NTP 修正都會讓
    牆上時間往回跳，那會算出負的或荒謬的間隔。
    """
    global _last, _max_gap, _count
    now = time.monotonic()
    with _lock:
        if _last is not None:
            gap = now - _last
            if gap > _max_gap:
                _max_gap = gap
            for name, upper in _BUCKETS:
                if gap < upper:
                    _hist[name] += 1
                    break
        _last = now
        _count += 1


def stats() -> dict:
    """目前的統計。★ 這支不可以碰資料庫，也不可以呼叫 mark()。"""
    now = time.monotonic()
    with _lock:
        # 「現在正在進行中的這段空檔」也要算進去，否則一段長空檔要等到
        # 它結束（下一個請求進來）才看得到——而店家打烊後最長的那一段，
        # 正好是永遠不會結束的那一段。
        pending = (now - _last) if _last is not None else 0.0
        return {
            "max_gap_seconds": round(max(_max_gap, pending), 1),
            "current_gap_seconds": round(pending, 1),
            "distribution": dict(_hist),
            "sample_count": _count,
            "process_started_at": _started_at.isoformat(),
            "uptime_seconds": round(now - _started_mono, 1),
            "pid": os.getpid(),
            # 多 worker 時每個 worker 各有一份統計，看到的間隔會比真實的
            # 全域間隔長。附上 worker 數，讀的人才知道要不要打折。
            "workers": _worker_count(),
            "workers_note": (
                "單一 worker 時 max_gap_seconds 是精確值；多 worker 時"
                "請求被分散，這個數字是全域間隔的上界（偏保守）。"
            ),
            "neon_suspend_threshold_seconds": 300,
        }


def _worker_count() -> int | None:
    """部署時的 worker 數。讀不到就回 None，不要用預設值假裝知道。"""
    raw = os.environ.get("WEB_CONCURRENCY") or os.environ.get("GUNICORN_WORKERS")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None
