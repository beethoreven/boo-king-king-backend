"""這家店開了哪些功能。跨店家差異的唯一來源。

## 為什麼是一張表，不是環境變數

跨店家的架構是「一份程式碼、每店一套獨立的基礎設施」。店與店的差異放在
三個地方，分界線是「改了要不要重新部署」與「誰看得到」：

    環境變數   連線資訊、金鑰（DATABASE_URL、VARIANT、JOB_TOKEN）
    settings   會出現在使用者眼前的字（店名、網址、寄件人）
    functions  **行為**：收不收訂金、主持人怎麼指定、排期怎麼開放、卡什麼場

功能表的值會改變流程本身，所以不能散在程式碼的 if 裡——那樣「這家店到底
開了什麼」要靠讀 code 才答得出來，而且每加一家店就多一個分支。

## 沒有介面，用 SQL 改（同 settings、roles、abuse_rule）

    UPDATE functions SET value = 'off' WHERE key = 'deposit';

改完想立刻生效就呼叫 invalidate()，或等快取過期。

## 值寫錯會當場失敗

讀到不認得的值一律拋例外，不會退回預設值。理由是這幾個鍵**每一個都會
改變流程**：把 'assign' 打成 'asign' 而系統默默當成別的模式，症狀會是
「某些預約的狀態不對」，那要查很久。當場壞掉的話，改的人立刻知道。
"""

from __future__ import annotations

import os
import threading
import time

# 這家店的功能設定活多久。10 分鐘，理由同 db/roles.py：這張表六列、
# 幾乎不會變，而它被每一次預約寫入讀到。
_CACHE_TTL_SECONDS = float(os.environ.get("FUNCTION_CACHE_TTL_SECONDS", "600"))

# 每個鍵允許的值。讀到不在裡面的就是設定打錯了。
ALLOWED = {
    # 收不收訂金。off 的店家沒有「待收訂金」這個狀態——主持人確認完
    # （或根本不需要確認）就直接成立。
    "deposit": ("on", "off"),

    # 主持人怎麼指定：
    #   assign  必須指定，指定的人要按確認（步經徑）
    #   single  固定唯一主持人，建立時就指定好並視為已確認，不需要確認流程
    #   option  可以不指定；沒指定的角色不列入「還在等誰確認」
    "gm_mode": ("assign", "single", "option"),

    # 排期怎麼開放：
    #   positive  預設不開放，管理員設定的時段才開放（步經徑）
    #   negative  預設全開放，只有被關掉的時段不開放
    "schedule_mode": ("positive", "negative"),

    # 卡場條件：同一個時間點，什麼東西被佔用就不能再開一場。
    #   conflict_room    包廂（軟擋：管理員看得到是什麼撞到，仍可自行處理）
    #   conflict_gm      主持人（硬擋）
    #   conflict_script  同一齣劇本（硬擋）★ 尚未實作，值一定是 off
    "conflict_room": ("on", "off"),
    "conflict_gm": ("on", "off"),
    "conflict_script": ("on", "off"),
}

# single 模式下那位唯一的主持人。寫死 users.id = 1——一人店的第一個帳號
# 就是店主本人。
#
# ★ 案主 2026-09-11 明確承擔這個風險（我建議過不要依賴 id）：
#   「你不用擔心 DB 搬移意外，要真的發生我來改正」。會出事的情境是把資料
#   搬到另一個資料庫、而 id 1 落到別人身上；那時候症狀很明顯（每一場的
#   主持人都變成同一個陌生人），不是那種查不出來的錯。
SINGLE_GM_USER_ID = 1

_lock = threading.Lock()
_cache: dict[str, str] | None = None
_expires_at = 0.0


class FunctionError(RuntimeError):
    """功能表的值不合法。這是設定錯誤，不是使用者的錯。"""


def _load(cur) -> dict[str, str]:
    cur.execute("SELECT key, value FROM functions")
    got = {k: v for k, v in cur.fetchall()}
    for key, allowed in ALLOWED.items():
        if key not in got:
            raise FunctionError(
                f"functions 表缺少「{key}」。ensure_schema 會補上預設值，"
                f"出現這個錯誤代表那一列被刪掉了。"
            )
        if got[key] not in allowed:
            raise FunctionError(
                f"functions.{key} 的值是「{got[key]}」，只能是 {'、'.join(allowed)}"
            )
    return got


def all_functions(cur) -> dict[str, str]:
    """整張表，key -> value。帶快取。"""
    global _cache, _expires_at
    now = time.monotonic()
    with _lock:
        if _cache is not None and _expires_at > now:
            return _cache

    # ★ 查詢刻意放在鎖外面，理由同 db/roles.py：鎖只保護那個字典，
    #   包住網路操作的話，一次連線變慢就會讓所有請求一起卡住。
    loaded = _load(cur)

    with _lock:
        _cache = loaded
        _expires_at = time.monotonic() + _CACHE_TTL_SECONDS
    return loaded


def invalidate() -> None:
    """用 SQL 改完之後叫這支，下一次讀取就會重新載入。"""
    global _cache, _expires_at
    with _lock:
        _cache = None
        _expires_at = 0.0


def get(cur, key: str) -> str:
    if key not in ALLOWED:
        raise FunctionError(f"沒有「{key}」這個功能鍵")
    return all_functions(cur)[key]


def on(cur, key: str) -> bool:
    """on/off 型的鍵。"""
    value = get(cur, key)
    if value not in ("on", "off"):
        raise FunctionError(f"functions.{key} 不是 on/off 型的鍵（值是「{value}」）")
    return value == "on"


# ── 具名的讀法。呼叫端用這些，不要自己拼鍵名字串 ──────────────────

def deposit_required(cur) -> bool:
    return on(cur, "deposit")


def gm_mode(cur) -> str:
    return get(cur, "gm_mode")


def schedule_mode(cur) -> str:
    return get(cur, "schedule_mode")


def room_conflict_on(cur) -> bool:
    return on(cur, "conflict_room")


def gm_conflict_on(cur) -> bool:
    return on(cur, "conflict_gm")


def script_conflict_on(cur) -> bool:
    return on(cur, "conflict_script")
