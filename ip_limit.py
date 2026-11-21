"""依來源 IP 的限流：找出真正的來源 IP、一分鐘最多幾次、屢犯的封鎖一陣子。

用在「還不知道是誰」就要擋的地方：公開簡介頁（intro_public.py）、登入與註冊
（auth_utils/ip_guard.py）。已經知道是誰的請求另外有每個帳號的限流（auth_utils/guard.py）。

★ 狀態（Limiter）由呼叫端用 tenancy.PerTenant 保存：每家店一份，也是每個
  gunicorn worker 一份——兩個 worker 時實際門檻是設定值的兩倍。要精準就得多一個
  Redis，不划算（同 db/slot_cache.py 的取捨）。這一支只放演算法，不放任何狀態。

## 使用者的 IP 怎麼拿（2026-10-26 在 demo 實測過）

Render 前面有兩層代理：Cloudflare（172.71.x.x 這類）與 Render 自己的負載平衡
（10.x）。它們會把**使用者自己送來的 X-Forwarded-For 原封不動留在最前面**，
再接上真正的來源 IP。實測：

    沒帶標頭          36.227.139.114, 172.71.81.179, 10.29.8.12
    偽造 1.2.3.4      1.2.3.4,36.227.139.114, 172.71.81.179, 10.29.8.12

所以**第一段是使用者愛寫什麼就寫什麼**——拿它來限流，攻擊者每次換一個假 IP
就繞過了。正確的讀法是從右邊往左，跳過 Cloudflare 與私有網段的位址，遇到的
第一個就是真正的使用者（client_ip）。

★ 要是哪天 Cloudflare 多了新的網段而這裡沒更新，錯的方向是「把 Cloudflare 的
  位址當成使用者」——很多人會擠在同一個 IP 下一起被限流（看得見的 429），
  而不是讓攻擊者繞過。網段清單來自 https://www.cloudflare.com/ips/。

## IPv6 以 /64 為單位計算

一般一條 IPv6 連線拿到的是一整個 /64（2^64 個位址），使用者可以在裡面隨便換。
照完整位址算的話，每個請求換一個位址就永遠不會被限流。所以 IPv6 一律算到 /64
那一層（limit_key）。紀錄裡存的仍然是完整位址。
"""

from __future__ import annotations

import ipaddress
import threading
import time
from collections import deque

WINDOW_SECONDS = 60.0
# 10 分鐘內被擋 3 次就封鎖 10 分鐘。偶爾連按幾下重新整理不會被封；持續在打的會。
STRIKES_TO_BLOCK = 3
STRIKE_WINDOW_SECONDS = 600.0
BLOCK_SECONDS = 600.0
# 記憶體上限：一次記住的 IP 超過這麼多就先清掉閒置的，還是太多就整份清空。
# 清空等於暫時不限流（寧可放過，也不要讓這一層自己把記憶體吃光）。
MAX_TRACKED_KEYS = 10_000
# 存進紀錄的 X-Forwarded-For 原文最多幾個字（使用者送來的任意字串）。
MAX_HEADER_CHARS = 300

_lock = threading.Lock()


class Limiter:
    """一組限流的狀態。呼叫端用 tenancy.PerTenant(ip_limit.Limiter) 保存。"""

    __slots__ = ("hits", "strikes", "blocked_until")

    def __init__(self) -> None:
        self.hits: dict[tuple[str, str], deque] = {}     # (種類, 來源) -> 時間
        self.strikes: dict[str, deque] = {}               # 來源 -> 被擋的時間
        self.blocked_until: dict[str, float] = {}         # 來源 -> 封鎖到什麼時候


# 代理自己的位址：Cloudflare 公布的網段（https://www.cloudflare.com/ips/，2026-10-26）
# 加上私有網段（Render 內部的負載平衡是 10.x）。從右往左讀 X-Forwarded-For 時跳過這些。
_PROXY_NETS = tuple(ipaddress.ip_network(n) for n in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
))


def _is_proxy(value: str) -> bool:
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    if addr.is_private or addr.is_loopback or addr.is_link_local:
        return True
    return any(addr in net for net in _PROXY_NETS)


def client_ip(request) -> tuple[str, str]:
    """(拿來限流與記錄的 IP, 存進紀錄用的 X-Forwarded-For)。規則見檔案開頭。

    ★ 先完整解析、存的時候才截短，而且保留**右邊**：先截短再解析的話，攻擊者
      塞一長串假值就能把右邊可信的那一段擠掉。
    """
    header = request.headers.get("X-Forwarded-For") or ""
    parts = [p.strip() for p in header.split(",") if p.strip()]
    ip = None
    for candidate in reversed(parts):
        if not _is_proxy(candidate):
            ip = candidate
            break
    if ip is None:
        # 整串都是代理（或根本沒帶）。★ 不能退回第一段：那一段是使用者自己寫的
        # （例如從 Cloudflare Worker 發出的請求，整串都會落在 Cloudflare 網段，
        # 第一段就能任意指定）。取最右邊那段——它可能是代理自己的位址，那樣頂多
        # 是很多人擠在同一個 IP 下一起被限流，不會被偽造。沒帶標頭（本機開發、
        # 測試）就用連線的來源位址。
        ip = parts[-1] if parts else (request.remote_addr or "unknown")
    return ip[:64], header[-MAX_HEADER_CHARS:]


def limit_key(ip: str) -> str:
    """限流用的來源識別：IPv4 是位址本身，IPv6 是它所在的 /64（理由見檔案開頭）。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 6:
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return ip


def _prune(dq: deque, now: float, window: float) -> None:
    while dq and now - dq[0] > window:
        dq.popleft()


def check(state: Limiter, ip: str, kind: str, per_minute: int, *, block: bool = True) -> int | None:
    """記一次請求。被擋時回傳建議的重試秒數，否則 None。

    kind 把同一個來源的不同用途分開計數（例如簡介與圖片各有各的上限）。
    block=True：一分鐘內超過上限、10 分鐘內累積 STRIKES_TO_BLOCK 次的來源，封鎖
    BLOCK_SECONDS 秒（同一組 state 的所有 kind 一起封）。block=False 只限流不封鎖，
    給「同一個 IP 後面可能有很多真人」的地方用。
    """
    key = limit_key(ip)
    now = time.monotonic()
    with _lock:
        if block:
            until = state.blocked_until.get(key)
            if until is not None:
                if until > now:
                    return int(until - now) + 1
                del state.blocked_until[key]

        if len(state.hits) > MAX_TRACKED_KEYS:
            for k in [k for k, dq in state.hits.items() if not dq or now - dq[-1] > WINDOW_SECONDS]:
                del state.hits[k]
            if len(state.hits) > MAX_TRACKED_KEYS:
                state.hits.clear()
                state.strikes.clear()

        dq = state.hits.setdefault((kind, key), deque())
        _prune(dq, now, WINDOW_SECONDS)
        if len(dq) < per_minute:
            dq.append(now)
            return None

        if block:
            strikes = state.strikes.setdefault(key, deque())
            _prune(strikes, now, STRIKE_WINDOW_SECONDS)
            strikes.append(now)
            if len(strikes) >= STRIKES_TO_BLOCK:
                state.blocked_until[key] = now + BLOCK_SECONDS
                strikes.clear()
                return int(BLOCK_SECONDS)
        return max(1, int(WINDOW_SECONDS - (now - dq[0])) + 1)


def stats(state: Limiter) -> dict:
    """目前記住幾個來源、封鎖中幾個。不碰資料庫。"""
    with _lock:
        return {"tracked_ips": len({k for _kind, k in state.hits}),
                "blocked_ips": len(state.blocked_until)}
