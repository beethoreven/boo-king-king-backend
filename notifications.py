"""通知信：組信、排隊、寄出。

## 這支的第一原則：**寄信失敗不能影響任何主要流程**

一筆預約成不成立，跟通知信寄不寄得出去，是兩件事。Brevo 掛掉、網路慢、
API key 過期——這些都不該讓玩家看到「預約失敗」。所以這裡處處是「出事就
安靜放棄」，這在別的地方是壞味道，在這裡是規格：

  1. **組信在交易裡，寄信在交易外。** 組信要讀範本與收件人，需要 cursor；
     但真正的 HTTP 呼叫放到 commit 之後。順序反過來的話，一筆最後被
     rollback 掉的預約會留下一封「預約成功」的信，而信收不回來。
  2. **寄送在背景執行緒。** Brevo 正常約 300–800ms。放在請求裡等於每筆
     預約都變慢那麼多，而使用者完全不需要等這件事。
  3. **佇列有上限，滿了就丟。** 若 Brevo 長時間不通，寧可丟掉通知，也不要
     讓待寄的信無限堆積把記憶體吃光。
  4. **每一層都包 try/except。** 連「組信」本身都包——範本裡打錯一個變數
     不該讓預約 500。

## 為什麼是背景執行緒而不是工作佇列

正規做法是丟進 Redis/RabbitMQ 由 worker 消化，那能保證「進程重啟不掉信」。
但那要多一個永遠開著的服務，而這裡的量是一天個位數封通知。用執行緒的代價
只有一個：gunicorn 重啟時，還在佇列裡沒寄出的信會消失。以「通知」這個用途
來說可以接受——它不是帳務，掉了一封的後果是有人晚點才知道，不是資料錯。
★ 若哪天通知變成不可掉的東西（例如拿來寄付款連結），這個取捨要重新做。

## 開關有兩層

  * `roles.email_notification`——這個**角色**要不要收信。
  * `notification_template.enabled`——這**封**信要不要發。
兩層是不同的問題（「管理員收不收信」vs「記點通知要不要發」），所以沒有合併。

沒有設定 BREVO_API_KEY 時整支靜默停用——本機開發不會誤寄信給真人。
"""

from __future__ import annotations

import html
import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass

import requests

from db import roles

log = logging.getLogger(__name__)

BREVO_URL = "https://api.brevo.com/v3/smtp/email"
_API_KEY = os.environ.get("BREVO_API_KEY", "").strip()

# 單次 API 呼叫的逾時。Brevo 正常 300–800ms；給 10 秒是留給偶發的慢，
# 而不是等它復活——真的不通就該放棄下一封，不是卡在這封上。
_TIMEOUT_SECONDS = float(os.environ.get("BREVO_TIMEOUT_SECONDS", "10"))

# 佇列上限。超過就丟掉最新的那封並留一行 log。200 封遠大於任何正常尖峰
# （一天個位數），會塞滿只有一種可能：Brevo 長時間不通——那時候丟掉是對的。
_QUEUE_MAX = int(os.environ.get("NOTIFY_QUEUE_MAX", "200"))

_ROW_TTL_SECONDS = float(os.environ.get("NOTIFY_CACHE_TTL_SECONDS", "600"))


def enabled() -> bool:
    """有沒有在寄信。沒設 API key 就是沒有。"""
    return bool(_API_KEY)


# ── 設定與範本的快取 ────────────────────────────────────────────
#
# 做法與 db/roles.py 一模一樣：字典 + TTL + 鎖，查詢刻意放在鎖外面。
# 這兩張表加起來不到十列、幾乎不會變，而每寄一封信就要讀一次。

_lock = threading.Lock()
_settings_cache: dict[str, str] | None = None
_settings_expires = 0.0
_tpl_cache: dict[str, dict] | None = None
_tpl_expires = 0.0


def settings(cur) -> dict[str, str]:
    """站台設定，key -> value。帶快取。"""
    global _settings_cache, _settings_expires
    with _lock:
        if _settings_cache is not None and _settings_expires > time.monotonic():
            return _settings_cache
    cur.execute("SELECT key, value FROM settings")
    loaded = {k: (v or "") for k, v in cur.fetchall()}
    with _lock:
        _settings_cache = loaded
        _settings_expires = time.monotonic() + _ROW_TTL_SECONDS
    return loaded


def templates(cur) -> dict[str, dict]:
    """全部範本，key -> {subject, body, enabled}。帶快取。"""
    global _tpl_cache, _tpl_expires
    with _lock:
        if _tpl_cache is not None and _tpl_expires > time.monotonic():
            return _tpl_cache
    cur.execute("SELECT key, subject, body_html, enabled FROM notification_template")
    loaded = {
        r[0]: {"subject": r[1], "body": r[2], "enabled": r[3]} for r in cur.fetchall()
    }
    with _lock:
        _tpl_cache = loaded
        _tpl_expires = time.monotonic() + _ROW_TTL_SECONDS
    return loaded


def invalidate() -> None:
    """用 SQL 改完 settings 或 notification_template 之後想立刻生效就呼叫它。"""
    global _settings_cache, _settings_expires, _tpl_cache, _tpl_expires
    with _lock:
        _settings_cache = _tpl_cache = None
        _settings_expires = _tpl_expires = 0.0


# ── 代入變數 ────────────────────────────────────────────────────

_HOLE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render(text: str, values: dict) -> str:
    """把 {{name}} 換成 values['name']，**值會做 HTML 逃逸**。

    逃逸是必要的而不是保險：劇本名稱與使用者暱稱都是使用者輸入，裡面
    出現 & 或 < 的機率不低（「A&B」這種名字很常見）。不逃逸的話輕則版面
    壞掉，重則有人可以用暱稱往管理員的信箱塞任意 HTML。

    ★ 例外是 site_url：它要進 href，逃逸後 & 會變成 &amp; 而壞掉連結。
      但它來自 settings 表（只有案主用 SQL 改得到），不是使用者輸入，
      所以照原樣代入是安全的。

    找不到的變數留原樣不換——那是範本寫錯，讓它顯眼比悄悄變成空字串好，
    因為空字串會讓信看起來只是「少了一行」，沒有人會發現。
    """
    def sub(m):
        name = m.group(1)
        if name not in values:
            return m.group(0)
        raw = values[name]
        text_val = "" if raw is None else str(raw)
        return text_val if name == "site_url" else html.escape(text_val)

    return _HOLE.sub(sub, text)


@dataclass
class Message:
    """一封組好的信。**不含任何還要再查資料庫的東西**——寄件人也已經
    從 settings 讀好放進來了，因為背景執行緒沒有 cursor 可用。"""

    to: list[dict]      # [{"email": ..., "name": ...}]
    sender: dict        # {"email": ..., "name": ...}
    subject: str
    html_body: str


def build(cur, key: str, recipients: list[dict], values: dict) -> Message | None:
    """組一封信。任何一個條件不成立就回 None（沒有收件人、範本關掉、缺範本）。

    收件人是 [{"email", "name"}]；呼叫端負責篩過角色開關。
    """
    if not recipients:
        return None
    tpl = templates(cur).get(key)
    if tpl is None or not tpl["enabled"]:
        return None
    values = {**settings(cur), **values}
    return Message(
        to=[{"email": r["email"], "name": r.get("name") or r["email"]} for r in recipients],
        sender={
            "email": values.get("sender_email") or "",
            "name": values.get("sender_name") or values.get("site_name") or "",
        },
        subject=render(tpl["subject"], values),
        html_body=render(tpl["body"], values),
    )


# ── 收件人 ──────────────────────────────────────────────────────

# 「現在還算不算啟用」。跟 auth_utils/whitelist._fetch_user 用同一條算式，
# 而且必須同一條——
#
# 停權到期時 users.status 不會馬上變回 'active'：判定是讀取時即時算的
# （所以人一到期就能登入工作），寫回資料庫是每週排程才做的（guard.decay_points）。
# 中間最多差七天。這裡如果只看欄位字面值，那個人就會「照常使用系統、卻收不到
# 任何通知」，而且沒有任何錯誤——正是不會有人回報的那種壞法。
_EFFECTIVE_ACTIVE = """
    (CASE WHEN status = 'banned' AND banned_until IS NOT NULL AND banned_until <= NOW()
          THEN 'active' ELSE status END) = 'active'
"""


def _wants_email(cur, role_id: int) -> bool:
    return roles.wants(cur, role_id, "email")


def admins(cur) -> list[dict]:
    """要收信的管理員。**全部都寄**——管理員是唯一可能有複數收件人的角色
    （主持人各寄各的、玩家只有一位），所以這裡不挑人。

    停用或**目前仍在停權中**的不寄——他們現在不該在處理事情。已經到期的
    照寄，見 _EFFECTIVE_ACTIVE。
    """
    cur.execute(
        "SELECT id, email, name, role FROM users "
        f"WHERE role = 1 AND {_EFFECTIVE_ACTIVE}"
    )
    return [
        {"id": i, "email": e, "name": n}
        for i, e, n, role in cur.fetchall()
        if e and _wants_email(cur, role)
    ]


def users_by_id(cur, user_ids: list[int]) -> list[dict]:
    """指定的幾個人裡面，要收信的那些。

    ★ 依**每個人自己的角色**判斷開關，不是依「這封信是寄給主持人的」
      就套主持人的開關——管理員也可能被指定為某場的主持人。
    """
    ids = [i for i in user_ids if i]
    if not ids:
        return []
    cur.execute(
        "SELECT id, email, name, role FROM users "
        f"WHERE id = ANY(%s) AND {_EFFECTIVE_ACTIVE}",
        (ids,),
    )
    return [
        {"id": i, "email": e, "name": n}
        for i, e, n, role in cur.fetchall()
        if e and _wants_email(cur, role)
    ]


# ── 寄送 ────────────────────────────────────────────────────────

_outbox: queue.Queue | None = None
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
_sent = 0
_failed = 0
_dropped = 0


def _run() -> None:
    """背景執行緒。一封一封寄，永遠不讓例外逃出去終結自己。"""
    global _sent, _failed
    while True:
        msg = _outbox.get()
        try:
            resp = requests.post(
                BREVO_URL,
                headers={
                    "api-key": _API_KEY,
                    "content-type": "application/json",
                    "accept": "application/json",
                },
                json={
                    "sender": msg.sender,
                    "to": msg.to,
                    "subject": msg.subject,
                    "htmlContent": msg.html_body,
                },
                timeout=_TIMEOUT_SECONDS,
            )
            if resp.status_code // 100 == 2:
                _sent += 1
            else:
                _failed += 1
                # 內容不進 log：主旨含劇本名與玩家暱稱。狀態碼與 Brevo 的
                # 錯誤訊息才是排查要用的東西。
                log.warning("brevo %s: %s", resp.status_code, resp.text[:300])
        except Exception as exc:  # noqa: BLE001 - 寄信失敗只留紀錄
            _failed += 1
            log.warning("brevo 送出失敗: %s", exc)
        finally:
            _outbox.task_done()


def _ensure_worker() -> None:
    global _outbox, _worker
    with _worker_lock:
        if _outbox is None:
            _outbox = queue.Queue(maxsize=_QUEUE_MAX)
        # daemon=True：進程要結束時不等它。寧可掉一封通知，也不要讓
        # 部署卡在等一個寄信執行緒上。
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="brevo", daemon=True)
            _worker.start()


def dispatch(*messages) -> None:
    """把信交給背景執行緒。★ 一定要在 commit 之後呼叫。

    參數可以是 Message、None，或它們的 list——這樣呼叫端不必先過濾，
    「這次沒有要寄的信」直接傳 None 就好。
    """
    global _dropped
    flat: list[Message] = []
    for m in messages:
        if m is None:
            continue
        flat.extend(m if isinstance(m, list) else [m])
    if not flat or not enabled():
        return
    try:
        _ensure_worker()
        for msg in flat:
            try:
                _outbox.put_nowait(msg)
            except queue.Full:
                _dropped += 1
                log.warning("通知佇列已滿，丟棄一封")
    except Exception as exc:  # noqa: BLE001 - 寄信永遠不該拖垮呼叫端
        log.warning("通知排隊失敗: %s", exc)


def stats() -> dict:
    return {
        "enabled": enabled(),
        "sent": _sent,
        "failed": _failed,
        "dropped": _dropped,
        "queued": _outbox.qsize() if _outbox else 0,
    }
