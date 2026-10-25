"""公開簡介頁（不用登入）的防護：快取、每個 IP 的限流、瀏覽紀錄的暫存。

## 要防的是什麼

內容本來就是公開的，所以重點不是「別讓人看到」，而是**別讓人拿它來燒錢**：
每一次碰資料庫都會把那家店的 Neon 叫醒、至少醒 5 分鐘，月額度用完整家店停擺。
不用登入就打得到的頁面，等於任何人都能叫醒它。所以這一層的目標是：

  ★ **大部分流量完全不碰資料庫。** 一直重新整理、很多人同時看同一齣、
    亂猜不存在的劇本編號——都只打到記憶體。

## 四件事

  1. 簡介內容快取 60 秒（找不到的也快取，免得有人亂猜編號一直查資料庫）。
  2. 封面圖的位元組快取在記憶體（網址帶版本號，換圖就是新網址，不會舊）。
  3. 每個 IP 限流：簡介每分鐘 30 次、圖片每分鐘 120 次；10 分鐘內被擋 3 次的
     IP 封鎖 10 分鐘。全部在記憶體裡做，不寫資料庫。
  4. 瀏覽紀錄（誰、什麼時候、看了哪一齣）先記在記憶體，累積 200 筆或每天的
     排程跑時才一次寫進資料庫（db/intro.py 的 insert_views）。
     代價：服務重新啟動時，還沒寫進去的那幾筆會遺失。這份紀錄是用來追查
     攻擊的，不需要即時，這個取捨是刻意的。

★ 全部都是每家店一份（tenancy.PerTenant），也都是每個 gunicorn worker 一份：
  兩個 worker 時，實際的限流門檻是設定值的兩倍。這跟 db/slot_cache.py 的取捨
  一樣——要精準就得多一個 Redis，不划算。

## 使用者的 IP 怎麼拿

見 client_ip()。Render 的代理會在 X-Forwarded-For 加上它看到的來源 IP，但使用者
自己也可以先塞一個假的進去，所以**哪一段可信**取決於 Render 的代理怎麼加——
這件事要實測，不能照猜的寫（2026-10-26：文件沒寫清楚，社群說法互相矛盾）。
瀏覽紀錄一律另外存原始的整串，判斷規則改了也能回頭重新判讀。
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

import tenancy

INTRO_PER_MINUTE = int(os.environ.get("INTRO_RATE_PER_MINUTE", "30"))
COVER_PER_MINUTE = int(os.environ.get("INTRO_COVER_RATE_PER_MINUTE", "120"))
_WINDOW = 60.0
# 10 分鐘內被擋 3 次就封鎖 10 分鐘。偶爾連按幾下重新整理不會被封；持續在打的會。
_STRIKES_TO_BLOCK = 3
_STRIKE_WINDOW = 600.0
_BLOCK_SECONDS = 600.0
# 記憶體上限：一次記住的 IP 超過這麼多就先清掉閒置的，還是太多就整份清空。
# 清空等於暫時不限流（寧可放過，也不要讓這一層自己把記憶體吃光）。
_MAX_TRACKED_IPS = 10_000

INTRO_TTL_SECONDS = float(os.environ.get("INTRO_CACHE_TTL_SECONDS", "60"))
_MAX_INTRO_ENTRIES = 2_000
# 封面圖快取的總大小上限（每家店）。一張通常一兩百 KB，20MB 夠放上百張。
_MAX_COVER_BYTES = 20 * 1024 * 1024
COVER_TTL_SECONDS = 600.0

# 瀏覽紀錄：累積到這麼多筆就寫進資料庫；暫存最多這麼多筆，再多就丟掉新的。
FLUSH_AT = 200
_MAX_BUFFERED_VIEWS = 2_000
# 存進資料庫前截短。這兩個欄位是使用者送來的任意字串。
_MAX_HEADER_CHARS = 300

_lock = threading.Lock()

MISS = object()   # 快取沒有這一筆（跟「查過了，沒有這齣」的 None 分開）


class _Limiter:
    __slots__ = ("hits", "strikes", "blocked_until")

    def __init__(self) -> None:
        self.hits: dict[tuple[str, str], deque] = {}     # (種類, ip) -> 時間
        self.strikes: dict[str, deque] = {}               # ip -> 被擋的時間
        self.blocked_until: dict[str, float] = {}         # ip -> 封鎖到什麼時候


class _Caches:
    __slots__ = ("intro", "covers", "cover_bytes")

    def __init__(self) -> None:
        self.intro: dict[int, tuple[float, dict | None]] = {}
        self.covers: dict[tuple[int, int], tuple[float, bytes | None]] = {}
        self.cover_bytes = 0


class _Views:
    __slots__ = ("rows", "dropped")

    def __init__(self) -> None:
        self.rows: list[tuple] = []
        self.dropped = 0


_limiters = tenancy.PerTenant(_Limiter)
_caches = tenancy.PerTenant(_Caches)
_views = tenancy.PerTenant(_Views)


# ── IP ───────────────────────────────────────────────────────────


def client_ip(request) -> tuple[str, str]:
    """(拿來限流與記錄的 IP, 原始的 X-Forwarded-For 整串)。

    ★ 規則見檔案開頭「使用者的 IP 怎麼拿」。目前取 X-Forwarded-For 的第一段，
      沒有就用連線的來源位址。
    """
    chain = (request.headers.get("X-Forwarded-For") or "")[:_MAX_HEADER_CHARS]
    parts = [p.strip() for p in chain.split(",") if p.strip()]
    ip = parts[0] if parts else (request.remote_addr or "unknown")
    return ip[:64], chain


# ── 限流 ─────────────────────────────────────────────────────────


def _prune(dq: deque, now: float, window: float) -> None:
    while dq and now - dq[0] > window:
        dq.popleft()


def check_rate(ip: str, kind: str) -> int | None:
    """記一次請求。被擋時回傳建議的重試秒數，否則 None。kind 是 'intro' 或 'cover'。"""
    limit = INTRO_PER_MINUTE if kind == "intro" else COVER_PER_MINUTE
    lim = _limiters.get()
    now = time.monotonic()
    with _lock:
        until = lim.blocked_until.get(ip)
        if until is not None:
            if until > now:
                return int(until - now) + 1
            del lim.blocked_until[ip]

        if len(lim.hits) > _MAX_TRACKED_IPS:
            for key in [k for k, dq in lim.hits.items() if not dq or now - dq[-1] > _WINDOW]:
                del lim.hits[key]
            if len(lim.hits) > _MAX_TRACKED_IPS:
                lim.hits.clear()
                lim.strikes.clear()

        dq = lim.hits.setdefault((kind, ip), deque())
        _prune(dq, now, _WINDOW)
        if len(dq) < limit:
            dq.append(now)
            return None

        strikes = lim.strikes.setdefault(ip, deque())
        _prune(strikes, now, _STRIKE_WINDOW)
        strikes.append(now)
        if len(strikes) >= _STRIKES_TO_BLOCK:
            lim.blocked_until[ip] = now + _BLOCK_SECONDS
            strikes.clear()
            return int(_BLOCK_SECONDS)
        return max(1, int(_WINDOW - (now - dq[0])) + 1)


# ── 快取 ─────────────────────────────────────────────────────────


def cached_intro(mmg_id: int):
    """快取裡的簡介（可能是 None＝查過了沒有這齣）。沒快取到回 MISS。"""
    c = _caches.get()
    with _lock:
        hit = c.intro.get(mmg_id)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
    return MISS


def store_intro(mmg_id: int, payload: dict | None) -> None:
    c = _caches.get()
    with _lock:
        if len(c.intro) >= _MAX_INTRO_ENTRIES:
            c.intro.clear()
        c.intro[mmg_id] = (time.monotonic() + INTRO_TTL_SECONDS, payload)


def cached_cover(mmg_id: int, version: int):
    c = _caches.get()
    with _lock:
        hit = c.covers.get((mmg_id, version))
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
    return MISS


def store_cover(mmg_id: int, version: int, data: bytes | None) -> None:
    c = _caches.get()
    size = len(data) if data else 0
    with _lock:
        if c.cover_bytes + size > _MAX_COVER_BYTES:
            c.covers.clear()
            c.cover_bytes = 0
        old = c.covers.get((mmg_id, version))
        if old is not None and old[1]:
            c.cover_bytes -= len(old[1])
        c.covers[(mmg_id, version)] = (time.monotonic() + COVER_TTL_SECONDS, data)
        c.cover_bytes += size


def invalidate() -> None:
    """管理員改了劇本或簡介之後呼叫：目前這家店的簡介與圖片快取全部作廢。

    不只清那一齣：上下架、價錢、訂金開關都會改變簡介頁的內容，而劇本只有
    幾十齣，整份重建的成本可以忽略，換到的是不必記得每一種改動影響哪幾筆。
    """
    c = _caches.get()
    with _lock:
        c.intro.clear()
        c.covers.clear()
        c.cover_bytes = 0


# ── 瀏覽紀錄 ─────────────────────────────────────────────────────


def record_view(mmg_id: int, ip: str, chain: str, user_agent: str) -> bool:
    """記一筆瀏覽（先放在記憶體）。回傳 True 代表累積夠了，呼叫端該 flush。"""
    v = _views.get()
    row = (mmg_id, ip, chain or None, (user_agent or "")[:_MAX_HEADER_CHARS] or None,
           datetime.now(timezone.utc))
    with _lock:
        if len(v.rows) >= _MAX_BUFFERED_VIEWS:
            v.dropped += 1
            return True
        v.rows.append(row)
        return len(v.rows) >= FLUSH_AT


def take_views() -> list[tuple]:
    """把目前這家店暫存的瀏覽紀錄全部拿出來（交給 db/intro.insert_views 寫入）。"""
    v = _views.get()
    with _lock:
        rows, v.rows = v.rows, []
    return rows


def give_back(rows: list[tuple]) -> None:
    """寫入失敗時放回去，下一次再試（超過上限的部分丟掉）。"""
    v = _views.get()
    with _lock:
        room = _MAX_BUFFERED_VIEWS - len(v.rows)
        v.rows[:0] = rows[:max(0, room)]
        v.dropped += max(0, len(rows) - room)


def stats() -> dict:
    """目前這家店的暫存狀況（給 /internal/idle-gap-stats）。不碰資料庫。"""
    v, c, lim = _views.get(), _caches.get(), _limiters.get()
    with _lock:
        return {
            "buffered_views": len(v.rows),
            "dropped_views": v.dropped,
            "cached_intros": len(c.intro),
            "cached_cover_bytes": c.cover_bytes,
            "tracked_ips": len(lim.hits),
            "blocked_ips": len(lim.blocked_until),
        }
