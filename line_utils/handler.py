"""一則 LINE 事件 → 做什麼、回什麼。

這一層刻意不碰 HTTP 也不碰 requests：路由負責驗簽與取出事件，client 負責
送訊息，這裡只決定「這則訊息代表什麼、該回哪一句」。分開之後這一層測得
起來——不需要 LINE 憑證、不需要網路。
"""

from __future__ import annotations

import logging
import uuid

from db import line_template
from db.bookings import (
    REASON_GM_BUSY,
    REASON_NOT_OPEN,
    REASON_OVERLAP,
    REASON_SAME_TIME,
    REASON_STORE_BUSY,
    BookingError,
    BookingResult,
    create_booking,
)

from line_utils import commands
from line_utils.images import ImageReply

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


def _owner_name(cur) -> str:
    """LINE 訂場掛名的那個帳號叫什麼——沒權限的人被告知「請聯繫誰」。

    ★ 從 users 表讀，不寫在回覆文字的預設值裡。後端是每一家店共用的程式，
      預設值寫「請聯繫eli」的話，這句話也會被種進其他店家的資料庫。店家
      想寫死一個名字，直接 UPDATE line_template 那一列就好。
    """
    cur.execute("SELECT name FROM users WHERE id = %s", (STORE_ACCOUNT_ID,))
    row = cur.fetchone()
    name = (row[0] or "").strip() if row else ""
    # 名字空著時不要送出「請聯繫。」或「請聯繫None。」
    return name or "店家"


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


def handle_group_message(cur, event: dict) -> tuple[str | ImageReply, BookingResult | None]:
    """群組裡 tag 我的那一則：先把 @mention 挖掉，剩下的交給 handle_command。"""
    msg = event["message"]
    text = commands.strip_mentions(msg.get("text") or "", mentionees_of(event))
    sender = (event.get("source") or {}).get("userId")
    return handle_command(cur, text, event["webhookEventId"], sender=sender)


def handle_command(cur, text: str, webhook_event_id: str, *,
                   sender: str | None) -> tuple[str | ImageReply, BookingResult | None]:
    """處理一段已經清乾淨的指令文字，回傳 (要回覆的東西, 訂到的那一筆)。

    要回覆的東西多半是一句話；查詢指令（某一天、行事曆）回的是 ImageReply，
    由 app.py 換成圖片訊息——網址要在請求裡才組得出來，這一層不碰 HTTP。

    第二個值沒訂到就是 None；訂到了是 create_booking 的結果——呼叫端要靠它的
    already_existed 分辨「這次新訂的」與「LINE 重送同一個事件、先前就訂好的」，
    後者不該再推播、再寄信。

    群組（tag 之後）與私訊共用這一段——兩條路的差別只在「怎麼判斷是在跟
    bot 講話」，**誰有資格、指令規則、回覆文字全部一樣**。分開寫的話，哪天
    改了其中一條，另一條就會忘了跟著改。

    ★ 訂沒訂到不能靠看第一個值的內容推。回覆文字是店家自己在
      line_template 裡改的，用「開頭是不是 ✅」來判斷成不成功的話，她
      哪天把那個 emoji 拿掉，推播就會安靜地停掉——而「通知沒發出去」
      這種事沒有人會回報。

    ★ 不論成功或失敗都回傳一句話——tag 了 bot 卻沒有任何反應，使用者
      無從判斷是「格式錯」還是「bot 死了」。唯一不回的情況是伺服器
      根本沒醒（那時候這支不會被呼叫到），那沒辦法。
    """
    # ★ 權限排在**所有判斷之前**（案主 2026-09-14）。
    #
    #   只有店家人員能跟 bot 互動，群組與私訊都一樣：不是店家人員的，不管傳
    #   什麼（「預訂 …」、「哈囉」、格式打錯）一律回 no_permission。
    #
    #   排在最前面也代表沒權限的人拿不到「找不到這齣」「那個時間已經有場」
    #   這些回覆——那些等於在告訴他劇本名單與場次狀況。
    #
    #   （同一天稍早的版本是先看「是不是預訂」，不是預訂的回「我不知道你在說
    #   什麼」；案主改成不管說什麼都回沒有權限。）
    if not is_staff(cur, sender):
        return line_template.render(cur, "no_permission", owner_name=_owner_name(cur)), None

    kind, payload = commands.parse(text)

    if kind == commands.UNKNOWN:
        return line_template.render(cur, "unknown"), None
    if kind == commands.BAD_DATETIME:
        return line_template.render(cur, "bad_datetime"), None
    if kind == commands.BAD_DATE:
        return line_template.render(cur, "bad_date"), None
    if kind == commands.BAD_RANGE:
        return line_template.render(cur, "bad_range", max_months=commands.MAX_MONTHS), None
    if kind == commands.BAD_CALENDAR:
        return line_template.render(cur, "bad_calendar"), None

    # 查詢：這裡只決定畫什麼，真的畫是在 LINE 來抓圖的時候（見 images.py）。
    # 資格已經在最前面查過了——沒權限的人連圖的網址都拿不到。
    if kind == commands.DAY:
        return ImageReply.day(payload["date"]), None
    if kind == commands.CALENDAR:
        return ImageReply.months(payload["months"]), None

    name = payload["name"]
    cur.execute(
        "SELECT id, name FROM mmg WHERE lower(btrim(name)) = lower(btrim(%s))",
        (name,),
    )
    row = cur.fetchone()
    if row is None:
        return line_template.render(cur, "script_not_found", name=name), None
    mmg_id, mmg_name = row

    # ★ 這裡把 BookingError 接住、翻成回覆文字、不往外拋——所以這一段依賴
    #   create_booking 的保證：**拋錯的時候什麼都沒寫**（先查再寫，見那支的
    #   說明）。那個保證是 2026-09-14 補的；在那之前它是「先寫、滿了才拋」，
    #   靠呼叫端 rollback，而這裡沒有 rollback，結果就是回覆說「已經有場次
    #   了」、資料庫卻多一筆。哪天 create_booking 又改回先寫後查，這裡要跟著
    #   補一道 SAVEPOINT。
    try:
        result = create_booking(
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
        return _failure_text(cur, exc), None

    return line_template.render(
        cur, "booked",
        mmg_name=mmg_name,
        date=payload["date"].strftime("%Y/%-m/%-d"),
        time=payload["time"].strftime("%H:%M"),
    ), result


# 被拒的原因代碼 → 回哪一句（line_template 的 key）。
#
# ★ 看的是 BookingError.reason，**不是錯誤訊息的文字**。原本這裡是拿
#   「無法預訂」「已經被預約」這些中文片語去比對，那樣後端哪天為了網頁改一
#   個字，這邊就會悄悄分錯類，而且不會報錯（2026-09-14 改掉）。
#
#   template key 的 conflict 沿用舊名：店家可能已經改過那一列的字，換成新
#   key 的話她改過的內容會被晾在一列沒人讀的資料上。
_REASON_TEMPLATE = {
    REASON_SAME_TIME: "conflict",     # ❌ 這個時間已經有場次了
    REASON_OVERLAP: "overlap",        # ❌ 與其他場次所需時間重疊
    REASON_NOT_OPEN: "not_open",      # ❌ 這個時間沒有開放
    REASON_STORE_BUSY: "store_busy",  # ❌ 當天有其他的排程，請換一個日期
    REASON_GM_BUSY: "gm_busy",        # ❌ 當天沒有開放帶場
}


# 需要店主名字的那幾句。忙碌日兩句都是在說「那天那個人不接」，所以要帶名字。
_NEEDS_OWNER = ("store_busy", "gm_busy")


def _failure_text(cur, exc: BookingError) -> str:
    key = _REASON_TEMPLATE.get(exc.reason)
    if key is None:
        # ★ 沒有原因代碼的拒絕（例如劇本設定有問題）。回 conflict 是退而求其次，
        #   而且**一定要記 log**：安靜地回錯一句話是最難發現的——群組裡看到
        #   「已經有場次了」的人不會想到那其實是別的原因。
        log.warning("LINE：沒有原因代碼的訂場失敗，回了 conflict。原訊息：%s", exc)
        key = "conflict"
    if key in _NEEDS_OWNER:
        return line_template.render(cur, key, owner_name=_owner_name(cur))
    return line_template.render(cur, key)


def is_staff(cur, line_user_id: str | None) -> bool:
    """這個 LINE 帳號能不能透過 bot 訂場（群組與私訊都看這一支）。

    三個條件都要：LINE 帳號綁定到系統上某個使用者、那個人是管理員或主持人
    （role <= 2）、帳號是啟用中。

    ## 群組與私訊都限定身分

    LINE 訂的場一律掛在店主名下（STORE_ACCOUNT_ID），所以能訂場的人必須是
    店家自己人。私訊任何人加了好友都能傳；群組裡則是玩家也在——案主
    2026-09-14 定：玩家在群組 tag 也不能用；不是店家人員的訊息，不管內容
    一律回 no_permission 那句。
    （更早的版本群組是誰都能訂，理由是「群組是店家邀的私人群組」，已改掉。）

    ## 為什麼是「綁定＋角色」而不是把某個 id 寫在程式裡

    ★ 誰能用是資料，不是程式。寫死 id 的話，每加一個人就要改 code、部署；
      而且不同店家的 LINE userId 是各自獨立的空間，寫死的值換一家店就錯。
      用 users 表判斷，開放給誰就是一句 SQL。

    ★ 要知道的副作用：line_user_id 同時也是推播的收件人依據。所以「為了
      收推播而綁定」的店家人員，**也會同時取得透過 bot 訂場的資格**。兩件事目前
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


def user_id_for(cur, line_user_id: str | None) -> int | None:
    """綁定了這個 LINE 帳號的使用者 id；沒綁就是 None。"""
    if not line_user_id:
        return None
    cur.execute("SELECT id FROM users WHERE line_user_id = %s", (line_user_id,))
    row = cur.fetchone()
    return row[0] if row else None


def push_targets(cur, *, exclude_user_id: int | None = None) -> list[str]:
    """要收私訊通知的人的 LINE userId。

    ★ 兩個條件都要：角色的 line_notification 開著（roles 表，店家自己
      用 SQL 控），而且那個人真的填了 line_user_id（加過 bot 好友才拿
      得到）。少任何一個就不是有效的收件人。
    """
    cur.execute(
        """
        SELECT u.id, u.line_user_id
          FROM users u JOIN roles r ON r.id = u.role
         WHERE u.status = 'active'
           AND r.line_notification
           AND u.line_user_id IS NOT NULL
        """
    )
    # exclude_user_id：讓這筆預約成立的那個人（網站、後台、LINE 都一樣）。
    #
    # 推播的用途是「讓店家人員知道**別人**訂了場」。做這件事的人就是收件人時，
    # 那則推播只是在告訴他一件他剛剛自己做的事，而且每一則都算進每月額度。
    #
    # ★ 群組原本**不**排除（理由是群組回覆與私訊推播是不同頻道），案主
    #   2026-09-14 改成排除，同一天再擴大到網站與後台：「不管是我或 eli 預約，
    #   就排除」。用使用者 id 比對而不是 LINE userId，網站上的動作才比得到。
    return [line_id for uid, line_id in cur.fetchall() if uid != exclude_user_id]
