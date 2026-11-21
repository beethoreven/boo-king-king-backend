"""公開簡介頁（不用登入）的防護：快取、每個 IP 的限流、瀏覽紀錄的暫存。

## 要防的是什麼

內容本來就是公開的，所以重點不是「別讓人看到」，而是**別讓人拿它來燒錢**：
每一次碰資料庫都會把那家店的 Neon 叫醒、至少醒 5 分鐘，月額度用完整家店停擺。
不用登入就打得到的頁面，等於任何人都能叫醒它。所以這一層的目標是：

  ★ **大部分流量完全不碰資料庫。** 一直重新整理、很多人同時看同一齣、
    亂猜不存在的劇本編號——都只打到記憶體。

## 五件事

  1. 簡介內容快取 60 秒（找不到的也快取，免得有人亂猜編號一直查資料庫）。
  2. 封面與角色圖的位元組快取在記憶體（網址帶版本號，換圖就是新網址，不會舊）。
     ★ 簡介內容沒快取到、去查資料庫的那一次，順便把這一齣所有的圖一起撈進來
       （db/intro.public_images）。不這樣做的話，一齣 10 個角色的劇本，一個訪客
       就要查 12 次資料庫，第 5 點的每日上限幾個人就用完了。
  3. 每個 IP 限流：簡介每分鐘 30 次、圖片每分鐘 120 次；10 分鐘內被擋 3 次的
     IP 封鎖 10 分鐘。全部在記憶體裡做，不寫資料庫。怎麼找出真正的來源 IP、
     怎麼計數，在 ip_limit.py（登入與註冊的限流也用同一套）。
  4. 瀏覽紀錄（誰、什麼時候、看了哪一齣）先記在記憶體，累積 200 筆或每天的
     排程跑時才一次寫進資料庫（db/intro.py 的 insert_views）。
     代價：服務重新啟動時，還沒寫進去的那幾筆會遺失。這份紀錄是用來追查
     攻擊的，不需要即時，這個取捨是刻意的。
  5. **每家店每天最多讓公開頁查 100 次資料庫**（INTRO_DB_BUDGET_PER_DAY）。
     限流是每個 IP 各自算的，擋不住「很多不同 IP 各打幾次、每次猜一個沒查過的
     編號」——那種流量每一次都是快取沒有的，每一次都會叫醒資料庫。這一道是
     總量的上限：用完之後，快取裡沒有的頁面暫時回 503，台北時間午夜歸零。
     快取命中的瀏覽不算次數，照常顯示；管理員的編輯畫面不經過這裡。
     （預估簡介頁一天約 10 次瀏覽，100 很充足。）

★ 全部都是每家店一份（tenancy.PerTenant），也都是每個 gunicorn worker 一份：
  兩個 worker 時，實際的限流門檻是設定值的兩倍。這跟 db/slot_cache.py 的取捨
  一樣——要精準就得多一個 Redis，不划算。

★ 瀏覽紀錄另外存 X-Forwarded-For 的原始整串（保留右邊可信的那一段），判讀規則
  （見 ip_limit.py）要改也能回頭重新判讀。
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

import logging

import ip_limit
import tenancy

log = logging.getLogger(__name__)

INTRO_PER_MINUTE = int(os.environ.get("INTRO_RATE_PER_MINUTE", "30"))
COVER_PER_MINUTE = int(os.environ.get("INTRO_COVER_RATE_PER_MINUTE", "120"))

INTRO_TTL_SECONDS = float(os.environ.get("INTRO_CACHE_TTL_SECONDS", "60"))
_MAX_INTRO_ENTRIES = 2_000
# 圖片快取的總大小上限（每家店）。封面一兩百 KB、角色圖幾十 KB，一齣全部約 1MB，
# 20MB 夠放二十齣。超過就整份清空（下一次看簡介時會重新撈進來）。
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_TTL_SECONDS = 600.0

# 每家店每天最多讓公開簡介頁查幾次資料庫（見檔案開頭第 5 點）。每個 worker 各算各的。
DB_BUDGET_PER_DAY = int(os.environ.get("INTRO_DB_BUDGET_PER_DAY", "100"))
_TAIPEI = timezone(timedelta(hours=8))

# 瀏覽紀錄：累積到這麼多筆就寫進資料庫；暫存最多這麼多筆，再多就丟掉新的。
FLUSH_AT = 200
_MAX_BUFFERED_VIEWS = 2_000
# 存進資料庫前截短。這兩個欄位是使用者送來的任意字串。
_MAX_HEADER_CHARS = 300

_lock = threading.Lock()

MISS = object()   # 快取沒有這一筆（跟「查過了，沒有這齣」的 None 分開）


class _Caches:
    __slots__ = ("intro", "images", "image_bytes")

    def __init__(self) -> None:
        self.intro: dict[int, tuple[float, dict | None]] = {}
        # key 是 ("cover", 劇本 id, 版本) 或 ("character", 劇本 id, 角色 id, 版本)。
        self.images: dict[tuple, tuple[float, bytes | None]] = {}
        self.image_bytes = 0


class _Views:
    __slots__ = ("rows", "dropped")

    def __init__(self) -> None:
        self.rows: list[tuple] = []
        self.dropped = 0


class _Budget:
    __slots__ = ("day", "used", "warned")

    def __init__(self) -> None:
        self.day = None
        self.used = 0
        self.warned = False


_limiters = tenancy.PerTenant(ip_limit.Limiter)
_budgets = tenancy.PerTenant(_Budget)
_caches = tenancy.PerTenant(_Caches)
_views = tenancy.PerTenant(_Views)


# ── 限流 ─────────────────────────────────────────────────────────


def check_rate(ip: str, kind: str) -> int | None:
    """記一次請求。被擋時回傳建議的重試秒數，否則 None。kind 是 'intro' 或 'cover'。"""
    limit = INTRO_PER_MINUTE if kind == "intro" else COVER_PER_MINUTE
    return ip_limit.check(_limiters.get(), ip, kind, limit)


# ── 每天的資料庫查詢上限 ─────────────────────────────────────────


def _today():
    return datetime.now(_TAIPEI).date()


def take_db_budget() -> int | None:
    """快取沒有、要去查資料庫之前先問這裡。可以查回 None；今天的額度用完了
    回傳離台北午夜還有幾秒（給 Retry-After）。"""
    b = _budgets.get()
    today = _today()
    with _lock:
        if b.day != today:
            b.day, b.used, b.warned = today, 0, False
        if b.used < DB_BUDGET_PER_DAY:
            b.used += 1
            return None
        first = not b.warned
        b.warned = True
    if first:
        # 一天只記一次：這是「有人在大量打簡介頁」的訊號，值得在 log 裡看見，
        # 但每一個被擋的請求都記一行只會把 log 洗掉。
        log.warning("簡介頁今天的資料庫查詢已達上限（%s 次），之後只回快取", DB_BUDGET_PER_DAY)
    now = datetime.now(_TAIPEI)
    midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), _TAIPEI)
    return max(1, int((midnight - now).total_seconds()))


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


def cached_image(key: tuple):
    """快取裡的圖片位元組（可能是 None＝查過了沒有這張）。沒快取到回 MISS。"""
    c = _caches.get()
    with _lock:
        hit = c.images.get(key)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
    return MISS


def store_image(key: tuple, data: bytes | None) -> None:
    c = _caches.get()
    size = len(data) if data else 0
    with _lock:
        if c.image_bytes + size > _MAX_IMAGE_BYTES:
            c.images.clear()
            c.image_bytes = 0
        old = c.images.get(key)
        if old is not None and old[1]:
            c.image_bytes -= len(old[1])
        c.images[key] = (time.monotonic() + IMAGE_TTL_SECONDS, data)
        c.image_bytes += size


def invalidate() -> None:
    """管理員改了劇本或簡介之後呼叫：目前這家店的簡介與圖片快取全部作廢。

    不只清那一齣：上下架、價錢、訂金開關都會改變簡介頁的內容，而劇本只有
    幾十齣，整份重建的成本可以忽略，換到的是不必記得每一種改動影響哪幾筆。
    """
    c = _caches.get()
    with _lock:
        c.intro.clear()
        c.images.clear()
        c.image_bytes = 0


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
    v, c, b = _views.get(), _caches.get(), _budgets.get()
    with _lock:
        local = {
            "db_budget_per_day": DB_BUDGET_PER_DAY,
            "db_budget_used_today": b.used if b.day == _today() else 0,
            "buffered_views": len(v.rows),
            "dropped_views": v.dropped,
            "cached_intros": len(c.intro),
            "cached_image_bytes": c.image_bytes,
        }
    return {**local, **ip_limit.stats(_limiters.get())}
