"""角色的名稱與通知開關，唯一的來源。

在這張表出現之前，角色名稱散在四個地方各有一份：後端的 ROLE_LABEL、
schema 的 CHECK 約束、seed_demo 的印表、前端的下拉選項。改一個地方要記得
改另外三個，而漏掉的那一個不會報錯——只會讓同一個角色在不同畫面上顯示
不一樣的字。

## 為什麼要快取

角色名稱出現在每一份使用者清單上，而這張表只有三列、幾乎不會變。每次列
清單都去查一次是白費一趟往返（同區約 30ms，但更重要的是它會把 Neon 從休眠
叫醒——見 db/idle_gap.py）。

做法跟 auth_utils/whitelist.py 那份權限快取一樣：字典 + TTL + 鎖。TTL 給得
比權限長很多，因為改角色名稱這種事一年不會有一次，而權限撤銷要求幾十秒內
生效。

★ 用 SQL 改完想立刻生效的話呼叫 invalidate()，或等 TTL 過。
"""

from __future__ import annotations

import os
import threading
import time

# 角色設定活多久。10 分鐘：改名字或改通知開關是罕見的管理動作，等十分鐘
# 生效完全可以接受，而換到的是「列使用者清單不必多查一次資料庫」。
_CACHE_TTL_SECONDS = float(os.environ.get("ROLE_CACHE_TTL_SECONDS", "600"))

_lock = threading.Lock()
_cache: dict[int, dict] | None = None
_expires_at = 0.0


def _load(cur) -> dict[int, dict]:
    cur.execute(
        "SELECT id, name, line_notification, email_notification FROM roles ORDER BY id"
    )
    return {
        r[0]: {"id": r[0], "name": r[1], "line": r[2], "email": r[3]}
        for r in cur.fetchall()
    }


def all_roles(cur) -> dict[int, dict]:
    """全部角色，id -> {id, name, line, email}。帶快取。"""
    global _cache, _expires_at
    now = time.monotonic()
    with _lock:
        if _cache is not None and _expires_at > now:
            return _cache

    # ★ 刻意在鎖外面查資料庫。鎖只保護那個字典，不能包住網路操作——
    #   一旦把查詢放進鎖裡，任何一次連線變慢都會讓所有等著讀角色的請求
    #   一起卡住。同樣的理由見 auth_utils/whitelist.get_user。
    loaded = _load(cur)

    with _lock:
        _cache = loaded
        _expires_at = time.monotonic() + _CACHE_TTL_SECONDS
    return loaded


def label(cur, role_id: int) -> str:
    """這個角色叫什麼。查不到就回數字本身——顯示用的東西不該讓請求失敗。"""
    row = all_roles(cur).get(role_id)
    return row["name"] if row else str(role_id)


def wants(cur, role_id: int, channel: str) -> bool:
    """這個角色要不要收某個管道的通知。channel 是 'email' 或 'line'。

    查不到角色時回 False——不確定要不要發的時候，不發比亂發好。
    """
    row = all_roles(cur).get(role_id)
    return bool(row and row.get(channel))


def invalidate() -> None:
    """清掉快取。用 SQL 改完 roles 之後想立刻生效就呼叫它。"""
    global _cache, _expires_at
    with _lock:
        _cache = None
        _expires_at = 0.0
