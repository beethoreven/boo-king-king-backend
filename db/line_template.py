"""LINE bot 的回覆文字。

跟 db/roles.py 同一個形狀：一張小表、行程內快取、沒有介面，用 SQL 改。

## 為什麼不跟 notification_template 共用

那張是 email 的（subject + body_html）。LINE 回覆是純文字、沒有主旨，
塞進去會讓兩個欄位都在說謊。說明見 db/schema.py 的 _ensure_line_template。

## 變數展開失敗要看得見

文字是店家自己改的，改壞是可能的——少一個大括號、打錯變數名。那時
**當場拋出來**，不要送出一段半殘的字：群組裡出現「已排定《{mmg_nam」
沒有人知道是設定錯了，只會覺得 bot 壞了。
"""

from __future__ import annotations

import os
import threading
import time

# 跟 roles / settings 同一個理由：這張表六列、幾乎不會變，而它被每一次
# 回覆讀到。
_CACHE_TTL_SECONDS = float(os.environ.get("LINE_TEMPLATE_CACHE_TTL_SECONDS", "600"))

_lock = threading.Lock()
_cache: dict[str, str] | None = None
_expires_at = 0.0


class LineTemplateError(RuntimeError):
    """回覆文字缺了或展不開。這是設定錯誤，不是使用者的錯。"""


def _load(cur) -> dict[str, str]:
    cur.execute("SELECT key, text FROM line_template")
    return {k: v for k, v in cur.fetchall()}


def all_templates(cur) -> dict[str, str]:
    global _cache, _expires_at
    now = time.monotonic()
    with _lock:
        if _cache is not None and _expires_at > now:
            return _cache

    # 查詢放在鎖外面，理由同 db/roles.py：鎖只保護那個字典，包住網路
    # 操作的話，一次連線變慢會讓所有請求一起卡住。
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


def render(cur, key: str, **values) -> str:
    """取出一句回覆並填入變數。

    缺鍵或展不開都直接拋——那是設定錯誤，要讓改的人立刻知道。
    """
    text = all_templates(cur).get(key)
    if text is None:
        raise LineTemplateError(
            f"line_template 缺少「{key}」。ensure_schema 會補上預設值，"
            f"出現這個錯誤代表那一列被刪掉了。"
        )
    try:
        return text.format(**values)
    except (KeyError, IndexError, ValueError) as exc:
        raise LineTemplateError(
            f"line_template.{key} 展不開：{exc}。"
            f"可用的變數是 {sorted(values)}，大括號要成對。"
        ) from exc
