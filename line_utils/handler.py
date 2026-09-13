"""一則 LINE 事件 → 做什麼、回什麼。

這一層刻意不碰 HTTP 也不碰 requests：路由負責驗簽與取出事件，client 負責
送訊息，這裡只決定「這則訊息代表什麼、該回哪一句」。分開之後這一層測得
起來——不需要 LINE 憑證、不需要網路。
"""

from __future__ import annotations

import logging
import uuid

from db import line_template
from db.bookings import BookingError, create_booking

from line_utils import commands

log = logging.getLogger(__name__)

# 冪等鍵的命名空間。
#
# ★ booking_record.request_id 是 **UUID 欄位**，而 LINE 的 webhookEventId
#   是 26 碼的 ULID 形狀，塞不進去。uuid5 把它換算成 UUID，而且是
#   **確定性的**：同一個事件 id 算幾次都一樣，所以 LINE 重送幾次都只會
#   有一筆預約。
#
# ★ 跟前端產生的不可能相撞：uuid5 是 v5、crypto.randomUUID() 是 v4，
#   版本位就不同。命名空間另外再隔一層，別的系統就算也用 uuid5，只要
#   命名空間不同就落在不同空間。
_LINE_NS = uuid.uuid5(uuid.NAMESPACE_URL, "boo-king-king/line-webhook")

# LINE 訂的場掛在誰名下。
#
# ★ 這家店的 LINE 流程是「店主在群組裡幫玩家訂」（案主 2026-09-14），
#   所以預約的 player_id 就是店主本人，不需要把群組成員跟系統帳號綁定。
#   群組裡是誰要求的目前不記——案主說先不做。
#
# ★ 跟 functions.SINGLE_GM_USER_ID 剛好是同一個人，但**意義不同**：那個是
#   「誰主持」，這個是「這筆紀錄掛在誰名下」。哪天有店家要分開（例如有
#   一個專門的『櫃檯』帳號），就是改這裡，不會誤動主持人那條。
STORE_ACCOUNT_ID = 1


def request_id_for(webhook_event_id: str) -> str:
    """LINE 事件 id → 可以放進 booking_record.request_id 的 UUID。"""
    return str(uuid.uuid5(_LINE_NS, webhook_event_id))


def is_group_text_to_me(event: dict) -> bool:
    """這則事件是不是「群組裡有人 tag 我、而且是文字訊息」。

    ★ 判斷 tag 用 mentionees[].isSelf，不是比對文字。比對文字會被「訊息
      裡剛好提到 bot 的名字」誤觸，而 isSelf 是 LINE 明確告訴我們的。
    """
    if event.get("type") != "message":
        return False
    msg = event.get("message") or {}
    if msg.get("type") != "text":
        return False
    if (event.get("source") or {}).get("type") != "group":
        return False
    mentionees = ((msg.get("mention") or {}).get("mentionees")) or []
    return any(m.get("isSelf") is True for m in mentionees)


def mentionees_of(event: dict) -> list[dict]:
    msg = event.get("message") or {}
    return ((msg.get("mention") or {}).get("mentionees")) or []


def handle_group_message(cur, event: dict) -> tuple[str, bool]:
    """處理一則「群組裡 tag 我」的訊息，回傳 (要回覆的字, 有沒有真的訂到)。

    ★ 第二個值不能靠看第一個值的內容推。回覆文字是店家自己在
      line_template 裡改的，用「開頭是不是 ✅」來判斷成不成功的話，她
      哪天把那個 emoji 拿掉，推播就會安靜地停掉——而「通知沒發出去」
      這種事沒有人會回報。

    ★ 不論成功或失敗都回傳一句話——群組裡 tag 了 bot 卻沒有任何反應，
      使用者無從判斷是「格式錯」還是「bot 死了」。唯一不回的情況是
      伺服器根本沒醒（那時候這支不會被呼叫到），那沒辦法。
    """
    msg = event["message"]
    text = commands.strip_mentions(msg.get("text") or "", mentionees_of(event))
    kind, payload = commands.parse(text)

    if kind == commands.UNKNOWN:
        return line_template.render(cur, "unknown"), False
    if kind == commands.BAD_DATETIME:
        return line_template.render(cur, "bad_datetime"), False

    name = payload["name"]
    cur.execute(
        "SELECT id, name FROM mmg WHERE lower(btrim(name)) = lower(btrim(%s))",
        (name,),
    )
    row = cur.fetchone()
    if row is None:
        return line_template.render(cur, "script_not_found", name=name), False
    mmg_id, mmg_name = row

    try:
        create_booking(
            cur,
            mmg_id=mmg_id,
            player_id=STORE_ACCOUNT_ID,
            session_date=payload["date"],
            session_time=payload["time"],
            # single 模式下後端自己填主持人，這裡不表達意見。
            gm_user_ids=[None, None, None, None],
            request_id=request_id_for(event["webhookEventId"]),
        )
    except BookingError as exc:
        # 後端的訊息是寫給管理員看的（帶劇本名、主持人名）。群組裡有別的
        # 客人，所以這裡換成店家自己寫的那幾句，不把內部細節倒出去。
        return _failure_text(cur, str(exc)), False

    return line_template.render(
        cur, "booked",
        mmg_name=mmg_name,
        date=payload["date"].strftime("%Y/%-m/%-d"),
        time=payload["time"].strftime("%H:%M"),
    ), True


# 後端錯誤 → 要回哪一句。比對的是那幾句錯誤訊息裡穩定的片語。
#
# ★ 對不到就回 conflict（最常見的那一種），而且**記一筆 log**。安靜地
#   回錯一句話是最難發現的——群組裡看到「已經有場次了」的人不會想到
#   那其實是別的原因。
_FAILURE_HINTS = (
    ("未開放", "not_open"),
    ("已經過了", "not_open"),
    ("未開放預約", "not_open"),
    ("衝突", "conflict"),
    ("無法預定", "conflict"),
    ("已經被預約", "conflict"),
    ("額滿", "conflict"),
)


def _failure_text(cur, message: str) -> str:
    for needle, key in _FAILURE_HINTS:
        if needle in message:
            return line_template.render(cur, key)
    log.warning("LINE：分類不到的訂場失敗，回了 conflict。原訊息：%s", message)
    return line_template.render(cur, "conflict")


def push_targets(cur) -> list[str]:
    """要收私訊通知的人的 LINE userId。

    ★ 兩個條件都要：角色的 line_notification 開著（roles 表，店家自己
      用 SQL 控），而且那個人真的填了 line_user_id（加過 bot 好友才拿
      得到）。少任何一個就不是有效的收件人。
    """
    cur.execute(
        """
        SELECT u.line_user_id
          FROM users u JOIN roles r ON r.id = u.role
         WHERE u.status = 'active'
           AND r.line_notification
           AND u.line_user_id IS NOT NULL
        """
    )
    return [r[0] for r in cur.fetchall()]
