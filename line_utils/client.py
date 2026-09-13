"""呼叫 LINE Messaging API。

## 兩支端點，成本與時機都不同

    Reply  回應剛剛那一則訊息。**不計入免費額度**，但 replyToken 有時效
           而且只能用一次 → **同步送**，不能丟背景。
    Push   主動私訊。**吃每月 200 則** → **丟背景佇列**，不要擋在 webhook
           的 5 秒預算裡。

## 沒設定就整支停用

同 notifications.py：缺任一個環境變數就 enabled() 回 False，呼叫端不必
各自判斷。本機開發不會誤發訊息給真人。
"""

from __future__ import annotations

import logging
import os
import queue
import threading

import requests

log = logging.getLogger(__name__)

_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "").strip()
_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
_TIMEOUT = float(os.environ.get("LINE_TIMEOUT_SECONDS", "5"))

_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
_PUSH_URL = "https://api.line.me/v2/bot/message/push"

# 背景佇列上限。滿了就丟掉最新的那一則並記一筆——推播是輔助，永遠不該
# 拖垮訂場那條路（同 notifications.py 的取捨）。
_QUEUE_MAX = int(os.environ.get("LINE_QUEUE_MAX", "100"))

_outbox: queue.Queue | None = None
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
_dropped = 0


def channel_secret() -> str:
    return _SECRET


def enabled() -> bool:
    """兩個環境變數都在才算開。"""
    return bool(_SECRET and _TOKEN)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_TOKEN}",
        "Content-Type": "application/json",
    }


def reply(reply_token: str, text: str) -> bool:
    """同步回覆。replyToken 只能用一次、而且有時效，所以不進佇列。

    回傳有沒有成功。**失敗只記 log 不拋**——webhook 已經處理完了（場次
    可能已經寫進去），這時候拋例外只會讓 LINE 收到 500 然後重送，而重送
    會帶著同一個 replyToken（已經用過或已過期），第二次一樣失敗。
    """
    if not enabled():
        return False
    try:
        r = requests.post(
            _REPLY_URL,
            json={"replyToken": reply_token, "messages": [{"type": "text", "text": text}]},
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        if r.status_code >= 400:
            log.warning("LINE reply 失敗 %s: %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - 回覆失敗不該拖垮訂場
        log.warning("LINE reply 例外: %s", exc)
        return False


def push(to_user_ids: list[str], text: str) -> None:
    """把私訊丟進背景佇列。★ 一定要在 commit 之後呼叫。

    不同步送的理由是 5 秒：webhook 的預算已經花在驗簽、查劇本、寫入、
    回覆上，推播是這條路徑上唯一可以延後的事。
    """
    ids = [u for u in (to_user_ids or []) if u]
    if not ids or not enabled():
        return
    global _dropped
    try:
        _ensure_worker()
        for uid in ids:
            try:
                _outbox.put_nowait((uid, text))
            except queue.Full:
                _dropped += 1
                log.warning("LINE 推播佇列已滿，丟棄一則")
    except Exception as exc:  # noqa: BLE001
        log.warning("LINE 推播排隊失敗: %s", exc)


def _ensure_worker() -> None:
    global _outbox, _worker
    with _worker_lock:
        if _outbox is None:
            _outbox = queue.Queue(maxsize=_QUEUE_MAX)
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="line-push", daemon=True)
            _worker.start()


def _run() -> None:
    while True:
        uid, text = _outbox.get()
        try:
            r = requests.post(
                _PUSH_URL,
                json={"to": uid, "messages": [{"type": "text", "text": text}]},
                headers=_headers(),
                timeout=_TIMEOUT,
            )
            if r.status_code >= 400:
                # 429 = 免費額度用完。這值得被看見，不要跟其他錯誤混在一起。
                if r.status_code == 429:
                    log.warning("LINE 推播額度可能已用完（429）：%s", r.text[:200])
                else:
                    log.warning("LINE push 失敗 %s: %s", r.status_code, r.text[:300])
        except Exception as exc:  # noqa: BLE001
            log.warning("LINE push 例外: %s", exc)
        finally:
            _outbox.task_done()


def profile_in_group(group_id: str, user_id: str) -> dict | None:
    """群組成員的公開資料（顯示名稱等）。

    ★ getProfile 只對「已加 bot 好友」的人有效，群組裡要用這一支。
      目前沒有呼叫點——「為誰訂」案主決定先不做（2026-09-14），留著是
      因為第 5 點確認可行時就是靠它，拿掉之後要再查一次文件。
    """
    if not enabled():
        return None
    try:
        r = requests.get(
            f"https://api.line.me/v2/bot/group/{group_id}/member/{user_id}",
            headers={"Authorization": f"Bearer {_TOKEN}"},
            timeout=_TIMEOUT,
        )
        return r.json() if r.status_code < 400 else None
    except Exception:  # noqa: BLE001
        return None
