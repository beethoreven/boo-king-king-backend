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
    """群組裡 tag 我的那一則：先把 @mention 挖掉，剩下的交給 handle_command。"""
    msg = event["message"]
    text = commands.strip_mentions(msg.get("text") or "", mentionees_of(event))
    return handle_command(cur, text, event["webhookEventId"])


def handle_command(cur, text: str, webhook_event_id: str) -> tuple[str, bool]:
    """處理一段已經清乾淨的指令文字，回傳 (要回覆的字, 有沒有真的訂到)。

    群組（tag 之後）與私訊（店家人員）共用這一段——兩條路的差別只在
    「怎麼判斷是在跟 bot 講話」與「誰有資格」，指令本身的規則一模一樣。
    分開寫的話，哪天改了錯誤訊息或格式，就會有一條路忘了改。

    ★ 第二個值不能靠看第一個值的內容推。回覆文字是店家自己在
      line_template 裡改的，用「開頭是不是 ✅」來判斷成不成功的話，她
      哪天把那個 emoji 拿掉，推播就會安靜地停掉——而「通知沒發出去」
      這種事沒有人會回報。

    ★ 不論成功或失敗都回傳一句話——tag 了 bot 卻沒有任何反應，使用者
      無從判斷是「格式錯」還是「bot 死了」。唯一不回的情況是伺服器
      根本沒醒（那時候這支不會被呼叫到），那沒辦法。
    """
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

    # ★ 這裡把 BookingError 接住、翻成回覆文字、不往外拋——所以這一段依賴
    #   create_booking 的保證：**拋錯的時候什麼都沒寫**（先查再寫，見那支的
    #   說明）。那個保證是 2026-09-14 補的；在那之前它是「先寫、滿了才拋」，
    #   靠呼叫端 rollback，而這裡沒有 rollback，結果就是回覆說「已經有場次
    #   了」、資料庫卻多一筆。哪天 create_booking 又改回先寫後查，這裡要跟著
    #   補一道 SAVEPOINT。
    try:
        create_booking(
            cur,
            mmg_id=mmg_id,
            player_id=STORE_ACCOUNT_ID,
            session_date=payload["date"],
            session_time=payload["time"],
            # single 模式下後端自己填主持人，這裡不表達意見。
            gm_user_ids=[None, None, None, None],
            request_id=request_id_for(webhook_event_id),
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


def is_staff(cur, line_user_id: str | None) -> bool:
    """這個 LINE 帳號能不能用私訊訂場。

    三個條件都要：LINE 帳號綁定到系統上某個使用者、那個人是管理員或主持人
    （role <= 2）、帳號是啟用中。

    ## 為什麼私訊要限定身分，群組不用

    LINE 訂的場一律掛在店主名下（STORE_ACCOUNT_ID）。群組是店家自己的私人
    群組，進得去的人本來就是她邀的；**私訊不是**——任何人加了 bot 好友都能
    私訊它。不擋的話，一個陌生人加好友、傳一句「預訂 …」，就能用店主的名義
    佔掉一個時段。

    ## 為什麼是「綁定＋角色」而不是把某個 id 寫在程式裡

    ★ 誰能用是資料，不是程式。寫死 id 的話，每加一個人就要改 code、部署；
      而且不同店家的 LINE userId 是各自獨立的空間，寫死的值換一家店就錯。
      用 users 表判斷，開放給誰就是一句 SQL。

    ★ 要知道的副作用：line_user_id 同時也是推播的收件人依據。所以「為了
      收推播而綁定」的店家人員，**也會同時取得私訊訂場的資格**。兩件事目前
      是綁在一起的；哪天要分開，是另一個開關的事。
    """
    if not line_user_id:
        return False
    cur.execute(
        "SELECT 1 FROM users "
        " WHERE line_user_id = %s AND status = 'active' AND role <= 2",
        (line_user_id,),
    )
    return cur.fetchone() is not None


def push_targets(cur, *, exclude: str | None = None) -> list[str]:
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
    # exclude：私訊訂場時的那個人。他剛在同一個私訊視窗收到回覆，再推一則
    # 一模一樣的過去只是重複。群組那條路不排除——群組裡的回覆大家都看得到，
    # 推播是另一個頻道（「有人訂了」的私人通知），不算重複。
    return [r[0] for r in cur.fetchall() if r[0] != exclude]
