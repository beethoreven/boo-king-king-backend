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

# 不做 HTML 逃逸的變數。只有「要進 href 而且不含使用者輸入」的才進得來。
_RAW_VALUES = ("site_url", "link")

# 每封信的按鈕要落到哪裡（前端路由，見 boo-king-king-web/js/route.js）。
#
# ★ **這是唯一寫著路由形狀的地方。** 不要把 ?view=... 寫進範本——範本存在
#   資料庫裡，寫死的話之後路由一改（查詢參數換成真路徑、或頁籤改名）就要
#   改每一列資料，而且改漏了不會有任何錯誤，只會有人點了信落在錯的地方。
#
# ★ 沒對到的 key 退回 site_url，也就是站台首頁——跟做路由之前一樣。新增
#   一封信而忘了加對應時，它會落在首頁（可以接受），而不是變成一個壞掉
#   的連結（不能接受）。
_BUTTON_TARGET = {
    "gm_assigned": "view=gm&tab=pending",
    "admin_reviewed": "view=admin&tab=bookings",
    "player_booked": "view=mybookings&tab=booked",
    "abuse_point": "view=admin&tab=abuse",
    "abuse_banned": "view=admin&tab=users",
    "admin_booked": "view=admin&tab=bookings&sub=booked",
}


def _check_button_targets() -> None:
    """_BUTTON_TARGET 必須**剛好**覆蓋所有範本，不多不少。

    ★ 這裡刻意不做「對不到就退回首頁」。退回首頁的話，新增一封信而忘了
      給它目的地時，信照樣寄得出去、按鈕照樣點得動，只是每個收件人都落在
      預約畫面——沒有錯誤、沒有人回報，而那正是這整件事一開始要修的毛病。
      寧可讓程式在啟動時就起不來：那時候只有工程師在看，而且一定看得到。
    """
    from db.default_templates import TEMPLATES

    missing = sorted(set(TEMPLATES) - set(_BUTTON_TARGET))
    extra = sorted(set(_BUTTON_TARGET) - set(TEMPLATES))
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"這幾封信沒有指定按鈕要去哪裡：{'、'.join(missing)}")
        if extra:
            parts.append(f"指定了目的地但沒有這封信：{'、'.join(extra)}")
        raise RuntimeError("通知信的按鈕目的地表對不上——" + "；".join(parts))


_check_button_targets()


def _button_link(site_url: str, key: str) -> str:
    """site_url 加上這封信該去的位置。

    site_url 自己帶查詢字串的話用 & 接——目前不會發生（settings 裡就是
    一個站台網址），但這一行比「之後有人加了參數才發現連結壞掉」便宜。

    ★ 用 [] 不用 .get()：key 對不到是程式錯誤，上面的 _check_button_targets()
      已經在啟動時擋掉了，能走到這裡還對不到的話要炸給人看。
    """
    if not site_url:
        return site_url
    return f"{site_url}{'&' if '?' in site_url else '?'}{_BUTTON_TARGET[key]}"


# 選填的列：<div data-if="note" …>…</div>。note 沒有值（None 或空字串）
# 就整列拿掉。範本用 default_templates._hrow(..., only_if=...) 產生這個形狀。
#
# ★ 只認「一層 div、裡面沒有別的 div」——範本裡的每一列本來就是這樣（標籤是
#   span、值是行內文字）。用 SQL 改範本時在這種列裡再塞 div 的話，這裡會切在
#   第一個 </div>，版面會壞得看得見，不會安靜地少一段。
_OPTIONAL_ROW = re.compile(r'<div\b([^>]*?)\s+data-if="(\w+)"([^>]*)>(.*?)</div>', re.S)


def render(text: str, values: dict, *, body: bool = False) -> str:
    """把 {{name}} 換成 values['name']，**值會做 HTML 逃逸**。

    逃逸是必要的而不是保險：劇本名稱與使用者暱稱都是使用者輸入，裡面
    出現 & 或 < 的機率不低（「A&B」這種名字很常見）。不逃逸的話輕則版面
    壞掉，重則有人可以用暱稱往管理員的信箱塞任意 HTML。

    ★ 例外是 site_url 與 link：它們要進 href。兩者都不是使用者輸入——
      site_url 來自 settings 表（只有案主用 SQL 改得到），link 是下面
      _button_link() 用 site_url 加一段寫死的查詢字串組出來的——所以
      照原樣代入是安全的。

    找不到的變數留原樣不換——那是範本寫錯，讓它顯眼比悄悄變成空字串好，
    因為空字串會讓信看起來只是「少了一行」，沒有人會發現。選填的列也一樣：
    data-if 指的變數根本沒給，那一列就留著（連 {{name}} 一起）。

    body=True（信的內文）時多做兩件事：拿掉沒有值的選填列、值裡的換行變成
    <br>——備註是 textarea 打的，換行是寫的人自己排的。逃逸之後才換，所以
    使用者打不出任何標籤。主旨不做：主旨裡不會有列，也不能有 <br>。
    """
    if body:
        def keep_or_drop(m):
            name = m.group(2)
            if name not in values:
                return m.group(0)
            if values[name] is None or values[name] == "":
                return ""
            return f"<div{m.group(1)}{m.group(3)}>{m.group(4)}</div>"

        text = _OPTIONAL_ROW.sub(keep_or_drop, text)

    def sub(m):
        name = m.group(1)
        if name not in values:
            return m.group(0)
        raw = values[name]
        text_val = "" if raw is None else str(raw)
        if name in _RAW_VALUES:
            return text_val
        escaped = html.escape(text_val)
        if body:
            escaped = escaped.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
        return escaped

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
    # 算在這裡而不是讓每個觸發點各自傳：build() 是每一封信都會經過的地方，
    # 所以 link 不可能漏掉。理由跟 default_templates._btn() 把警語綁在按鈕
    # 上是同一個——會漏掉而且漏了不會有人發現的事，要放在共用的那一層。
    values["link"] = _button_link(values.get("site_url") or "", key)
    return Message(
        to=[{"email": r["email"], "name": r.get("name") or r["email"]} for r in recipients],
        sender={
            "email": values.get("sender_email") or "",
            "name": values.get("sender_name") or values.get("site_name") or "",
        },
        subject=render(tpl["subject"], values),
        html_body=render(tpl["body"], values, body=True),
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
